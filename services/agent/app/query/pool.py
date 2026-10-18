"""有界查询连接池：受控会话、TLS 强制、等待者上限与不确定连接剔除。

设计原因：
- 不使用 aiomysql.create_pool，因为需要在认证前强制 TLS 的自定义连接类；
  本池只维护少量长驻连接，连接创建受总截止时间约束。
- 每条新连接都执行固定初始化并以只读基线查询确认：autocommit=1、
  transaction_read_only=1、max_execution_time 与 sql_mode/字符集符合预期；
  任一项不符即视为不可信连接，关闭而不是放回池。
- 超时、取消、协议异常或无法确认事务结束的连接一律剔除；正常释放只接受
  autocommit 仍为真的连接，避免把未结束事务留给后续请求。
- 池上限、等待者上限与获取超时共同约束并发；关闭时唤醒等待者并强制关闭
  全部已登记连接，不无限等待。
"""
from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiomysql

from app.adapters.driver_errors import is_tls_failure
from app.adapters.tls_connection import TlsEnforcingConnection
from app.datasource.config import DataSourceConfig
from app.errors import (
    DataSourceAuthError,
    DataSourceError,
    DataSourceNotConfigured,
    DataSourceTlsError,
    DataSourceUnavailable,
    QueryBusy,
    QueryTimeout,
)
from app.query.config import QueryLimits

# 固定会话模式：启用严格模式与 ONLY_FULL_GROUP_BY，避免宽松语义带来歧义。
SESSION_SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)

# 基线查询只读取会话状态，不访问业务数据；结果列顺序与 _verify_baseline 对应。
_BASELINE_SQL = (
    "SELECT @@SESSION.autocommit, @@SESSION.transaction_read_only, "
    "@@SESSION.max_execution_time, @@SESSION.sql_mode, "
    "@@SESSION.character_set_client, @@SESSION.character_set_connection, "
    "@@SESSION.character_set_results"
)

# 清理连接的时间上限；超时后同步 close 兜底，不把关闭流程拖入总预算。
_CLEANUP_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class PoolStats:
    """池状态快照，仅用于内部观测/测试，不对外暴露连接信息。"""

    total: int
    free: int
    used: int
    waiters: int
    creating: int


class QueryPool:
    """单个数据源的有界连接池。"""

    def __init__(
        self,
        config: DataSourceConfig,
        limits: QueryLimits,
        *,
        connector: Callable[[], Awaitable[Any]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._limits = limits
        self._connector = connector or self._default_connector
        self._clock = clock
        self._condition = asyncio.Condition()
        self._free: list[Any] = []
        self._used: set[Any] = set()
        self._all: set[Any] = set()
        self._creating = 0
        self._waiters = 0
        self._closed = False

    # ---- 获取与释放 ----

    async def acquire(self, *, deadline: float) -> Any:
        """获取连接；等待受获取超时与总截止共同约束。

        等待者数量上限防止无限排队；超时区分 QueryBusy（池忙）与
        QueryTimeout（请求总预算耗尽）。
        """
        acquire_until = self._now() + self._limits.pool_acquire_timeout_seconds

        async with self._condition:
            while True:
                if self._closed:
                    raise DataSourceUnavailable()

                if self._free:
                    conn = self._free.pop()
                    self._used.add(conn)
                    return conn

                if len(self._all) + self._creating < self._limits.pool_max_connections:
                    self._creating += 1
                    should_create = True
                    break

                if self._waiters >= self._limits.pool_max_waiters:
                    raise QueryBusy()

                now = self._now()
                wait_until = min(acquire_until, deadline)
                remaining = wait_until - now
                if remaining <= 0:
                    raise QueryTimeout() if deadline <= now else QueryBusy()

                self._waiters += 1
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._waiters -= 1

        conn = None
        try:
            conn = await self._create_connection(deadline)
        except BaseException:
            async with self._condition:
                self._creating -= 1
                self._condition.notify_all()
            if conn is not None:
                await self._force_close(conn)
            raise

        async with self._condition:
            self._creating -= 1
            if self._closed:
                self._condition.notify_all()
                close_after = True
            else:
                self._all.add(conn)
                self._used.add(conn)
                self._condition.notify_all()
                close_after = False

        if close_after:
            await self._force_close(conn)
            raise DataSourceUnavailable()
        return conn

    async def release(self, conn: Any) -> None:
        """归还健康连接；autocommit 不为真或已关闭的连接直接剔除。"""
        async with self._condition:
            if conn not in self._used:
                return
            self._used.discard(conn)
            healthy = not self._closed and not getattr(conn, "closed", False) and self._autocommit(conn)
            if healthy:
                self._free.append(conn)
            else:
                self._all.discard(conn)
            self._condition.notify()

        if not healthy:
            await self._force_close(conn)

    async def discard(self, conn: Any) -> None:
        """剔除连接：先从计数中移除，再优雅关闭并同步强制关闭兜底。"""
        async with self._condition:
            was_tracked = conn in self._used or conn in self._all or conn in self._free
            self._used.discard(conn)
            self._all.discard(conn)
            if conn in self._free:
                self._free.remove(conn)
            if was_tracked:
                self._condition.notify_all()
        await self._force_close(conn)

    async def close(self) -> None:
        """幂等关闭：唤醒等待者，强制关闭所有已登记连接（含使用中的）。"""
        async with self._condition:
            if self._closed:
                return
            self._closed = True
            connections = list({*self._free, *self._used, *self._all})
            self._free.clear()
            self._used.clear()
            self._all.clear()
            self._condition.notify_all()

        for conn in connections:
            await self._force_close(conn)

    def stats(self) -> PoolStats:
        """返回当前计数快照；不触发任何 I/O。"""
        return PoolStats(
            total=len(self._all),
            free=len(self._free),
            used=len(self._used),
            waiters=self._waiters,
            creating=self._creating,
        )

    # ---- 连接创建与初始化 ----

    async def _create_connection(self, deadline: float) -> Any:
        """在剩余预算内连接并初始化；失败/超时/取消都关闭半成品连接。"""
        remaining = deadline - self._now()
        if remaining <= 0:
            raise QueryTimeout()

        async def build() -> Any:
            conn = await self._connector()
            try:
                await self._initialize(conn)
            except BaseException:
                await self._force_close(conn)
                raise
            return conn

        try:
            return await asyncio.wait_for(build(), timeout=remaining)
        except asyncio.TimeoutError:
            raise QueryTimeout() from None
        except DataSourceError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._map_driver_error(exc) from None

    async def _initialize(self, conn: Any) -> None:
        """设置固定会话并做只读基线确认；任何一步失败都让连接失效。"""
        cursor = await conn.cursor()
        try:
            await cursor.execute("SET NAMES utf8mb4")
            await cursor.execute(f"SET SESSION sql_mode = '{SESSION_SQL_MODE}'")
            await cursor.execute(
                f"SET SESSION max_execution_time = {self._server_timeout_ms()}"
            )
            await cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            await cursor.close()

        baseline = await conn.cursor()
        try:
            await baseline.execute(_BASELINE_SQL)
            row = await baseline.fetchone()
        finally:
            await baseline.close()

        self._verify_baseline(row)

    def _verify_baseline(self, row: Any) -> None:
        """确认会话确实只读、无隐式事务且时限/字符集符合预期。"""
        if not row or len(row) < 7:
            raise DataSourceUnavailable()
        autocommit, read_only, max_execution_time, sql_mode, *charsets = row[:7]
        try:
            if int(autocommit) != 1:
                raise DataSourceUnavailable()
            if str(read_only).strip().upper() not in {"1", "ON"}:
                raise DataSourceUnavailable()
            if int(max_execution_time) != self._server_timeout_ms():
                raise DataSourceUnavailable()
        except (TypeError, ValueError):
            raise DataSourceUnavailable() from None

        if self._normalize_sql_mode(sql_mode) != self._normalize_sql_mode(SESSION_SQL_MODE):
            raise DataSourceUnavailable()
        if any(str(charset).lower() != "utf8mb4" for charset in charsets):
            raise DataSourceUnavailable()

    def _server_timeout_ms(self) -> int:
        return int(self._limits.server_timeout_seconds * 1000)

    @staticmethod
    def _normalize_sql_mode(value: Any) -> frozenset[str]:
        return frozenset(part.strip().upper() for part in str(value).split(",") if part.strip())

    # ---- 连接清理 ----

    async def _force_close(self, conn: Any) -> None:
        """有界优雅关闭后同步强制关闭；清理失败不抛出、不遮蔽主异常。"""
        try:
            await asyncio.wait_for(conn.ensure_closed(), timeout=_CLEANUP_TIMEOUT_SECONDS)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---- 默认连接器与错误映射 ----

    async def _default_connector(self) -> Any:
        """建立认证前强制 TLS 的连接（启用 TLS 时）并返回 aiomysql Connection。"""
        conn = TlsEnforcingConnection(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password.get_secret_value(),
            db=self._config.database,
            ssl=self._build_ssl_context(),
            connect_timeout=self._config.connect_timeout_seconds,
            autocommit=True,
            cursorclass=aiomysql.Cursor,
        )
        try:
            await conn._connect()
        except BaseException:
            self._force_close_sync(conn)
            raise
        return conn

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """TLS 开启时构建强校验上下文；关闭时返回 None，绝无自动降级。"""
        if not self._config.ssl_enabled:
            return None
        try:
            context = ssl.create_default_context(cafile=self._config.ssl_ca_path or None)
        except (OSError, ssl.SSLError) as exc:
            raise DataSourceTlsError() from exc
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    @staticmethod
    def _autocommit(conn: Any) -> bool:
        try:
            return bool(conn.get_autocommit())
        except Exception:
            return False

    def _map_driver_error(self, exc: Exception) -> DataSourceError:
        if is_tls_failure(exc):
            return DataSourceTlsError()
        code = self._mysql_code(exc)
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

    @staticmethod
    def _force_close_sync(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()
