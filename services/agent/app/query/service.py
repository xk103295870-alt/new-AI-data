"""QueryService：受限查询的唯一业务入口。

调用顺序（安全边界）：
1. 校验 source_id 存在且数据源已配置；
2. 校验授权白名单非空/合法；
3. 获取授权 Schema（复用 Task B 的 SchemaService 与缓存）；
4. QueryPolicy 解析与白名单校验（拒绝时不占用查询连接）；
5. QueryExecutor 执行策略生成的 SQL。

总预算使用 operation_budget 的绝对截止，覆盖 Schema 获取、排队、执行与读取；
审计日志只记录 request_id、数据源、结果分类、耗时与行数，不记录 SQL/常量/结果。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.config import Settings
from app.datasource.registry import DataSourceRegistry
from app.errors import DataSourceNotConfigured, DataSourceUnavailable, QueryTimeout
from app.operation_budget import operation_budget
from app.query.config import QueryLimits
from app.query.executor import QueryExecutor
from app.query.models import QueryResult, ValidatedQuery
from app.query.policy import QueryPolicy
from app.query.pool import QueryPool

logger = logging.getLogger(__name__)


class QueryService:
    """把数据源鉴权、Schema、策略与执行器组合为单一查询服务。"""

    def __init__(
        self,
        registry: DataSourceRegistry,
        policy: QueryPolicy,
        executor: QueryExecutor,
        *,
        limits: QueryLimits,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._executor = executor
        self._limits = limits
        self._clock = clock
        self._closed = False

    async def execute(self, source_id: str, sql: str, request_id: str) -> QueryResult:
        """执行一次受限查询。

        参数：
            source_id: 服务端预配置数据源 ID；未知 ID 返回 404。
            sql: 用户 SQL；只作为策略输入，执行的是策略生成的 SQL。
            request_id: 请求追踪 ID，仅用于审计返回，不参与 SQL。
        返回：
            QueryResult（列描述、行、row_count、truncated、duration_ms）。
        异常：
            领域异常均带稳定 code/http_status；文案固定且已脱敏。
        """
        if self._closed:
            raise DataSourceUnavailable()

        start = self._now()
        config = self._registry.get(source_id)
        self._registry.require_configured()
        self._registry.require_scope()

        schema_service = self._registry.service
        if schema_service is None:
            raise DataSourceNotConfigured()

        with operation_budget(self._limits.total_timeout_seconds) as deadline:
            try:
                async with asyncio.timeout_at(deadline):
                    summary, _fetched_at, _warnings, _cached = await schema_service.get_schema(
                        source_id
                    )
            except TimeoutError:
                raise QueryTimeout() from None

            query: ValidatedQuery = self._policy.validate(
                sql,
                scope=config.scope,
                schema=summary,
                database=config.database,
            )
            result = await self._executor.execute(query, deadline)

        duration_ms = max(0, int((self._now() - start) * 1000))
        result = result.model_copy(
            update={
                "request_id": request_id,
                "source_id": source_id,
                "duration_ms": duration_ms,
            }
        )
        self._log_success(request_id, source_id, result)
        return result

    async def close(self) -> None:
        """释放执行器与连接池；幂等，不关闭调用方拥有的注册表/适配器。"""
        if self._closed:
            return
        self._closed = True
        await self._executor.close()

    def _log_success(self, request_id: str, source_id: str, result: QueryResult) -> None:
        """仅记录非敏感执行元数据；绝不记录 SQL、常量、列值或凭据。"""
        logger.info(
            "query completed request_id=%s source_id=%s code=OK duration_ms=%s "
            "row_count=%s truncated=%s",
            request_id,
            source_id,
            result.duration_ms,
            result.row_count,
            result.truncated,
        )

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()


def build_query_service(
    settings: Settings,
    registry: DataSourceRegistry,
    *,
    connector: Callable[[], Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> QueryService | None:
    """按配置构建查询服务；数据源未配置时返回 None（不建立任何连接）。

    QueryConfigError 由调用方（应用工厂）捕获并降级为不可用；
    connector 仅用于测试注入，生产使用认证前强制 TLS 的默认连接器。
    """
    if not registry.config.configured:
        return None

    limits = QueryLimits.from_settings(settings)
    policy = QueryPolicy(
        max_rows=limits.max_rows,
        hard_max_rows=limits.hard_max_rows,
        max_sql_bytes=limits.max_sql_bytes,
    )
    pool = QueryPool(registry.config, limits, connector=connector, clock=clock)
    executor = QueryExecutor(pool, limits, clock=clock)
    return QueryService(registry, policy, executor, limits=limits, clock=clock)
