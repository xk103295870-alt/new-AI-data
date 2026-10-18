"""QueryExecutor：只执行已校验 SQL，并以流式、有界方式读取和序列化结果。

安全与资源边界：
- 只接受 ValidatedQuery；执行其 sql 字段（策略生成并二次校验过的 SQL），
  不接受原始用户输入。
- 使用服务端游标按块 fetchmany，绝不 fetchall；读取行数受 fetch_limit 约束，
  单元格/整包字节受配置上限约束，超限立即失败并剔除连接。
- 超时、取消、驱动异常、协议异常、序列化失败或游标关闭失败都视为连接状态
  不确定：丢弃连接，绝不归还池；只有完整读取且正常关闭游标才释放。
- 驱动错误映射为固定领域异常，不携带 SQL、常量、结果或原始驱动消息。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import aiomysql

from app.adapters.driver_errors import is_tls_failure
from app.errors import (
    DataSourceAuthError,
    DataSourceError,
    DataSourceNotConfigured,
    DataSourceTlsError,
    DataSourceUnavailable,
    QueryNotAllowed,
    QueryResultTooLarge,
    QueryTimeout,
)
from app.query.config import QueryLimits
from app.query.models import (
    QueryColumn,
    QueryResult,
    ValidatedQuery,
    serialize_value,
)
from app.query.pool import QueryPool

# 每次 fetchmany 的最大行数；控制单次内存峰值，不改变总行数上限。
_FETCH_CHUNK = 64


class QueryExecutor:
    """受限查询执行器；与 QueryPolicy、QueryPool 组合使用。"""

    def __init__(
        self,
        pool: QueryPool,
        limits: QueryLimits,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._pool = pool
        self._limits = limits
        self._clock = clock

    async def execute(self, query: ValidatedQuery, deadline: float) -> QueryResult:
        """执行已校验查询并返回结果 DTO。

        deadline 是单调时钟绝对截止（来自 QueryService 的总预算），覆盖排队、
        连接、执行、读取与游标关闭；任何超时/取消都先剔除连接再抛出。
        """
        conn = await self._pool.acquire(deadline=deadline)
        try:
            async with asyncio.timeout_at(deadline):
                rows, truncated = await self._read(conn, query)
        except TimeoutError:
            await self._pool.discard(conn)
            raise QueryTimeout() from None
        except asyncio.CancelledError:
            await self._pool.discard(conn)
            raise
        except DataSourceError:
            await self._pool.discard(conn)
            raise
        except Exception as exc:
            await self._pool.discard(conn)
            raise self._map_driver_error(exc) from None

        await self._pool.release(conn)
        return QueryResult(
            request_id="",
            source_id="",
            columns=[QueryColumn(name=c.name, data_type=c.data_type) for c in query.columns],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=0,
        )

    async def close(self) -> None:
        """释放池内全部连接；应用关闭时调用。"""
        await self._pool.close()

    # ---- 有界读取 ----

    async def _read(self, conn: Any, query: ValidatedQuery) -> tuple[list[list[Any]], bool]:
        """流式读取至多 fetch_limit 行，返回 (裁剪后的行, 是否被截断)。"""
        cursor = await conn.cursor(aiomysql.SSCursor)
        close_failed = False
        try:
            await cursor.execute(query.sql)

            rows: list[list[Any]] = []
            total_bytes = 0
            while len(rows) < query.fetch_limit:
                chunk = await cursor.fetchmany(min(_FETCH_CHUNK, query.fetch_limit - len(rows)))
                if not chunk:
                    break
                for raw_row in chunk:
                    if len(raw_row) != len(query.columns):
                        # 列数与校验结果不一致属于协议/结构异常，连接不可复用。
                        raise DataSourceUnavailable()
                    serialized, row_bytes, total_bytes = self._serialize_row(
                        raw_row, total_bytes
                    )
                    rows.append(serialized)
                    if len(rows) >= query.fetch_limit:
                        break
        finally:
            try:
                await cursor.close()
            except Exception:
                # 关闭失败说明服务端游标/连接状态不确定，交由调用方丢弃连接。
                close_failed = True

        if close_failed:
            raise DataSourceUnavailable()

        truncated = query.clamped and len(rows) > query.effective_limit
        if len(rows) > query.effective_limit:
            rows = rows[: query.effective_limit]
        return rows, truncated

    def _serialize_row(
        self,
        raw_row: Any,
        total_bytes: int,
    ) -> tuple[list[Any], int, int]:
        """序列化一行并累计字节；超限抛 QueryResultTooLarge（连接将被丢弃）。"""
        row: list[Any] = []
        row_bytes = 0
        for value in raw_row:
            converted, size = serialize_value(value)
            if size > self._limits.max_cell_bytes:
                raise QueryResultTooLarge()
            row.append(converted)
            row_bytes += size

        total = total_bytes + row_bytes
        if total > self._limits.max_result_bytes:
            raise QueryResultTooLarge()
        return row, row_bytes, total

    # ---- 错误映射 ----

    def _map_driver_error(self, exc: Exception) -> DataSourceError:
        """把驱动/协议异常映射为固定领域异常，绝不拼接原始消息。"""
        if is_tls_failure(exc):
            return DataSourceTlsError()
        code = self._mysql_code(exc)
        if code == 3024:  # max_execution_time 触发
            return QueryTimeout()
        if code in {1142, 1143}:
            return QueryNotAllowed()
        if code in {1044, 1045}:
            return DataSourceAuthError()
        if code == 1049:
            return DataSourceNotConfigured()
        return DataSourceUnavailable()

    @staticmethod
    def _mysql_code(exc: Exception) -> int | None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        return None
