"""查询执行资源上限：集中定义、启动时校验，非法配置默认不启用查询能力。

安全边界：所有上限都是硬约束；配置非法时抛 QueryConfigError，由应用工厂
降级为“查询服务不可用”，不以宽松默认值继续运行。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.config import Settings

# 资源参数的合理上界；超出视为配置错误，避免“看似有界、实际无限”。
_MAX_CONNECTIONS = 16
_MAX_WAITERS = 64
# 结果硬上限的固定天花板：即使配置更大，也不允许返回超过 1000 行。
_MAX_HARD_ROWS = 1000
_MAX_SQL_BYTES = 65536


class QueryConfigError(ValueError):
    """查询配置非法；消息固定，不含凭据或用户输入。"""


@dataclass(frozen=True)
class QueryLimits:
    """查询链路上的全部资源上限（秒/行/字节）。"""

    total_timeout_seconds: float = 10.0
    server_timeout_seconds: float = 8.0
    pool_max_connections: int = 2
    pool_max_waiters: int = 4
    pool_acquire_timeout_seconds: float = 2.0
    max_rows: int = 200
    hard_max_rows: int = 1000
    max_cell_bytes: int = 65536
    max_result_bytes: int = 1048576
    max_sql_bytes: int = 8192

    @classmethod
    def from_settings(cls, settings: Settings) -> "QueryLimits":
        """从 Settings 映射并校验；任何非法值都拒绝，不静默修正。

        总预算必须覆盖服务端执行上限；默认行数不得超过硬上限；
        单元格上限不得超过整包上限；连接/等待者数量有固定天花板。
        """
        limits = cls(
            total_timeout_seconds=settings.query_total_timeout_seconds,
            server_timeout_seconds=settings.query_server_timeout_seconds,
            pool_max_connections=settings.query_pool_max_connections,
            pool_max_waiters=settings.query_pool_max_waiters,
            pool_acquire_timeout_seconds=settings.query_pool_acquire_timeout_seconds,
            max_rows=settings.query_max_rows,
            hard_max_rows=settings.query_hard_max_rows,
            max_cell_bytes=settings.query_max_cell_bytes,
            max_result_bytes=settings.query_max_result_bytes,
            max_sql_bytes=settings.query_max_sql_bytes,
        )
        limits.validate()
        return limits

    def validate(self) -> None:
        """校验全部字段；错误消息只描述分类，不包含配置值。"""
        for value in (
            self.total_timeout_seconds,
            self.server_timeout_seconds,
            self.pool_acquire_timeout_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise QueryConfigError("查询超时参数必须是有限正数")

        if self.server_timeout_seconds > self.total_timeout_seconds:
            raise QueryConfigError("服务端执行上限不得超过总预算")
        if self.pool_acquire_timeout_seconds > self.total_timeout_seconds:
            raise QueryConfigError("连接等待上限不得超过总预算")

        if not 1 <= self.pool_max_connections <= _MAX_CONNECTIONS:
            raise QueryConfigError("查询连接数超出合法范围")
        if not 0 <= self.pool_max_waiters <= _MAX_WAITERS:
            raise QueryConfigError("查询等待者上限超出合法范围")

        if not 1 <= self.hard_max_rows <= _MAX_HARD_ROWS:
            raise QueryConfigError("结果硬上限超出合法范围")
        if not 1 <= self.max_rows <= self.hard_max_rows:
            raise QueryConfigError("默认结果行数必须为正且不超过硬上限")

        if self.max_cell_bytes <= 0 or self.max_result_bytes <= 0:
            raise QueryConfigError("结果大小上限必须是正整数")
        if self.max_cell_bytes > self.max_result_bytes:
            raise QueryConfigError("单单元格上限不得超过整包上限")

        if not 1 <= self.max_sql_bytes <= _MAX_SQL_BYTES:
            raise QueryConfigError("SQL 长度上限超出合法范围")
