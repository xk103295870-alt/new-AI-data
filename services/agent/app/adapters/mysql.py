"""DirectMySQLAdapter：用 aiomysql 执行固定 information_schema 元数据查询。

安全与资源边界：
- 只读 information_schema.TABLES/COLUMNS/TABLE_CONSTRAINTS/KEY_COLUMN_USAGE，
  连接检查用固定 SELECT 1；不读取业务数据行、视图定义或例程正文。
- schema/table/column 过滤值全部参数绑定（%s），禁止拼接用户或配置文本；
  固定 information_schema 表名避免动态业务标识符。
- 表名按数据库真实身份精确匹配（BINARY 字节比较，避免 collation 扩大匹配），
  列按“对象→允许列”配对收窄，不读取其他对象未授权列。
- 启用 TLS 时在发送认证材料前强制校验服务器声明 SSL 能力（TlsEnforcingConnection），
  证书链与主机名校验失败抛 DataSourceTlsError，绝不降级为明文。
- 每次操作新建连接并在 finally 关闭；优雅关闭设上限，最终同步强制关闭，
  取消/异常不把不确定状态连接回池，清理错误不遮蔽主失败原因。
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any, Awaitable, Callable

import aiomysql

from app.adapters.base import DataAdapter, RawMetadata
from app.adapters.driver_errors import is_tls_failure
from app.adapters.tls_connection import TlsEnforcingConnection
from app.datasource.config import AllowedScope, DataSourceConfig
from app.operation_budget import remaining_wait
from app.errors import (
    DataSourceAuthError,
    DataSourceError,
    DataSourceNotConfigured,
    DataSourceTimeout,
    DataSourceTlsError,
    DataSourceUnavailable,
)

# 认证/越权/库不存在等 MySQL 错误码，用于把驱动异常映射为稳定领域异常。
_AUTH_CODES = {1044, 1045, 1142, 1143}
_BAD_DB_CODE = 1049

# 固定元数据查询模板；{names}/{predicates} 仅填入 %s 占位符，从不填入真实名称。
_TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT "
    "FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = %s "
    "AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
    "AND BINARY TABLE_NAME IN {names} "
    "ORDER BY TABLE_NAME"
)
_COLUMNS_SQL = (
    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_COMMENT, ORDINAL_POSITION "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s AND ({predicates}) "
    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
)
_CONSTRAINTS_SQL = (
    "SELECT CONSTRAINT_NAME, TABLE_NAME, CONSTRAINT_TYPE "
    "FROM information_schema.TABLE_CONSTRAINTS "
    "WHERE TABLE_SCHEMA = %s "
    "AND BINARY TABLE_NAME IN {names} "
    "AND CONSTRAINT_TYPE IN ('PRIMARY KEY', 'FOREIGN KEY') "
    "ORDER BY TABLE_NAME, CONSTRAINT_NAME"
)
_KEY_USAGE_SQL = (
    "SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
    "REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
    "FROM information_schema.KEY_COLUMN_USAGE "
    "WHERE TABLE_SCHEMA = %s "
    "AND BINARY TABLE_NAME IN {names} "
    "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"
)
_CONNECTION_CHECK_SQL = "SELECT 1"


def _in_placeholders(count: int) -> str:
    """生成 `(%s, %s, ...)`，数量与参数一致；只包含占位符，不含任何名称。"""
    return "(" + ", ".join(["%s"] * count) + ")"


class DirectMySQLAdapter:
    """基于 aiomysql 的只读元数据适配器。

    连接通过可注入的 connector 建立（默认构造 TlsEnforcingConnection），便于测试用
    Fake Connection/Cursor 验证 SQL、绑定参数、映射与清理，而不替换整个 Adapter。
    """

    def __init__(
        self,
        config: DataSourceConfig,
        connector: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._config = config
        self._connector = connector or self._default_connector

    # ---- 对外协议 ----

    async def test_connection(self) -> None:
        """连接检查：建立连接、执行 SELECT 1，随后关闭连接。"""
        conn = await self._connect()
        try:
            await self._fetch(conn, _CONNECTION_CHECK_SQL, ())
        finally:
            await self._close_connection(conn)

    async def get_schema(self, scope: AllowedScope) -> RawMetadata:
        """读取授权范围内的 information_schema 元数据，不读取业务数据。"""
        conn = await self._connect()
        try:
            tables = await self._fetch(conn, _TABLES_SQL.format(names=_in_placeholders(len(scope.object_names()))), self._tables_params(scope))
            columns = await self._fetch(conn, self._columns_sql(scope), self._columns_params(scope))
            constraints = await self._fetch(
                conn,
                _CONSTRAINTS_SQL.format(names=_in_placeholders(len(scope.object_names()))),
                self._constraints_params(scope),
            )
            key_usage = await self._fetch(
                conn,
                _KEY_USAGE_SQL.format(names=_in_placeholders(len(scope.object_names()))),
                self._key_usage_params(scope),
            )
            return RawMetadata(tables=tables, columns=columns, constraints=constraints, key_usage=key_usage)
        finally:
            await self._close_connection(conn)

    async def close(self) -> None:
        """生命周期钩子；本适配器每操作即开即关，无长驻连接，故为空实现。"""
        return None

    # ---- 参数与 SQL 构建（全部参数绑定，表名精确、列名小写化）----

    def _tables_params(self, scope: AllowedScope) -> tuple[Any, ...]:
        return (self._config.database, *scope.object_names())

    def _columns_sql(self, scope: AllowedScope) -> str:
        """按“对象→该对象允许列”构造配对条件，避免读取其他对象未授权列。"""
        predicates = [
            "(BINARY TABLE_NAME = %s AND LOWER(COLUMN_NAME) IN {})".format(
                _in_placeholders(len(scope.lower_columns_for(name)))
            )
            for name in scope.object_names()
        ]
        return _COLUMNS_SQL.format(predicates=" OR ".join(predicates))

    def _columns_params(self, scope: AllowedScope) -> tuple[Any, ...]:
        params: list[Any] = [self._config.database]
        for name in scope.object_names():
            params.append(name)
            params.extend(scope.lower_columns_for(name))
        return tuple(params)

    def _constraints_params(self, scope: AllowedScope) -> tuple[Any, ...]:
        return (self._config.database, *scope.object_names())

    def _key_usage_params(self, scope: AllowedScope) -> tuple[Any, ...]:
        return (self._config.database, *scope.object_names())

    # ---- 连接与 TLS ----

    async def _default_connector(self) -> Any:
        ssl_context = self._build_ssl_context()
        conn = TlsEnforcingConnection(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password.get_secret_value(),
            db=self._config.database,
            ssl=ssl_context,
            connect_timeout=self._config.connect_timeout_seconds,
            autocommit=True,
            cursorclass=aiomysql.DictCursor,
        )
        try:
            await conn._connect()
        except BaseException:
            # 连接尚未返回时也归本层持有；失败或取消均同步关闭，不遗留半连接。
            self._force_close(conn)
            raise
        return conn

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """启用 TLS 时构建强校验上下文；关闭时返回 None（不发起 TLS）。

        校验证书链与主机身份，失败只能抛异常，绝无明文降级分支。
        CA 文件缺失/非法属于 TLS 配置错误，统一映射为 DataSourceTlsError。
        """
        if not self._config.ssl_enabled:
            return None
        try:
            context = ssl.create_default_context(cafile=self._config.ssl_ca_path or None)
        except (OSError, ssl.SSLError) as exc:
            raise DataSourceTlsError() from exc
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    async def _connect(self) -> Any:
        try:
            timeout = remaining_wait(self._config.connect_timeout_seconds)
            if timeout <= 0:
                raise DataSourceTimeout()
            return await asyncio.wait_for(self._connector(), timeout=timeout)
        except asyncio.TimeoutError:
            raise DataSourceTimeout() from None
        except DataSourceError:
            raise
        except Exception as exc:  # 驱动/网络/TLS 等，映射为脱敏领域异常
            raise self._map_driver_error(exc) from None

    # ---- 查询执行 ----

    async def _fetch(self, conn: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = None
        try:
            async def _run() -> list[dict[str, Any]]:
                nonlocal cursor
                cursor = await conn.cursor()
                await cursor.execute(sql, params)
                return await cursor.fetchall()

            try:
                timeout = remaining_wait(self._config.metadata_timeout_seconds)
                if timeout <= 0:
                    raise DataSourceTimeout()
                return await asyncio.wait_for(_run(), timeout=timeout)
            except asyncio.TimeoutError:
                raise DataSourceTimeout() from None
            except DataSourceError:
                raise
            except Exception as exc:
                raise self._map_driver_error(exc) from None
        finally:
            if cursor is not None:
                await self._close_cursor(conn, cursor)

    # ---- 连接清理 ----

    async def _close_connection(self, conn: Any) -> None:
        """优雅关闭只使用剩余总预算；取消/耗尽时直接同步关闭，不再追加等待。"""
        try:
            timeout = remaining_wait(self._config.connect_timeout_seconds)
            if timeout > 0:
                await asyncio.wait_for(conn.ensure_closed(), timeout=timeout)
        except Exception:
            pass  # 清理失败不遮蔽主异常；同步 close 是最终兜底，不是跳过清理。
        finally:
            self._force_close(conn)

    async def _close_cursor(self, conn: Any, cursor: Any) -> None:
        """游标排空同样受截止约束；取消或关闭失败时使连接失效并保留主异常。"""
        timeout = remaining_wait(self._config.connect_timeout_seconds)
        if timeout <= 0:
            self._force_close(conn)
            return
        try:
            await asyncio.wait_for(cursor.close(), timeout=timeout)
        except asyncio.CancelledError:
            self._force_close(conn)
            raise
        except Exception:
            self._force_close(conn)

    @staticmethod
    def _force_close(conn: Any) -> None:
        """同步关闭底层 transport；幂等，绝不抛异常。"""
        try:
            conn.close()
        except Exception:
            pass

    # ---- 错误映射 ----

    def _map_driver_error(self, exc: Exception) -> Any:
        if is_tls_failure(exc):
            return DataSourceTlsError()
        code = self._mysql_code(exc)
        if code in _AUTH_CODES:
            return DataSourceAuthError()
        if code == _BAD_DB_CODE:
            return DataSourceNotConfigured()
        if isinstance(exc, (ConnectionError, OSError)):
            return DataSourceUnavailable()
        # 其它驱动错误（含 MySQL 错误）保守归类为“不可用”；认证/越权/TLS 已在上方细分。
        return DataSourceUnavailable()

    @staticmethod
    def _mysql_code(exc: Exception) -> int | None:
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        return None
