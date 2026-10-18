"""应用工厂与 ASGI 入口。

create_app() 是唯一构建入口，测试可传入独立 Settings 与适配器工厂；
模块导入不联网、不要求外部凭据。数据源默认禁用，未配置时应用与 /health 仍可用。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.adapters.base import DataAdapter
from app.agent.business_terms import BusinessTermsError, default_business_terms, load_business_terms
from app.agent.config import AgentConfig, AgentConfigError
from app.agent.runner import AgentRunner
from app.agent.tools import ToolRegistry
from app.config import Settings
from app.datasource.config import DataSourceConfig, build_datasource_config
from app.datasource.registry import DataSourceRegistry
from app.errors import DataSourceError, InvalidRequest, error_envelope
from app.llm.provider import OpenAICompatibleProvider
from app.query.config import QueryConfigError
from app.query.service import build_query_service
from app.routes.chat import router as chat_router
from app.routes.datasources import router as datasources_router
from app.routes.health import router as health_router
from app.schema.cache import TTLCache
from app.schema.service import SchemaService
from app.session.store import SQLiteSessionStore

logger = logging.getLogger(__name__)


def _default_adapter_factory(config: DataSourceConfig) -> DataAdapter:
    """默认适配器工厂：构建 DirectMySQLAdapter；仅在已配置时被调用。"""
    from app.adapters.mysql import DirectMySQLAdapter

    return DirectMySQLAdapter(config)


def _build_registry(
    settings: Settings,
    adapter_factory: Callable[[DataSourceConfig], DataAdapter] | None = None,
) -> DataSourceRegistry:
    """按 Settings 构建注册表；未配置时不创建 adapter/service，避免启动时连接。"""
    factory = adapter_factory or _default_adapter_factory
    config = build_datasource_config(settings)
    registry = DataSourceRegistry(config=config, service=None)
    if config.configured:
        adapter = factory(config)
        cache = TTLCache(ttl_seconds=settings.schema_cache_ttl_seconds)
        registry.service = SchemaService(
            adapter,
            config.scope,
            cache,
            registry.status,
            database=config.database,
            total_timeout_seconds=config.total_timeout_seconds,
        )
    return registry


def _build_query_service(settings: Settings, registry: DataSourceRegistry):
    """构建内部查询服务；配置非法时记录分类告警并降级为不可用。

    查询配置错误不应阻止应用启动与 /health；但也不会以宽松默认值继续执行。
    """
    try:
        return build_query_service(settings, registry)
    except QueryConfigError:
        logger.warning("query service disabled: invalid query configuration")
        return None


def _build_agent(
    settings: Settings,
    registry: DataSourceRegistry,
    query_service,
    llm_provider,
    session_store,
) -> tuple[AgentRunner | None, SQLiteSessionStore | None]:
    """按配置构建 Agent；任何缺项/非法配置都降级为不可用，不阻止应用启动。

    - 未配置数据源查询服务时没有可信 SQL 入口，不构建 Agent；
    - LLM 三项配置缺一、上限非法或会话库不可创建时返回 (None, None)；
    - 注入 llm_provider/session_store 仅用于测试与显式替换。
    """
    if query_service is None:
        return None, None

    try:
        config = AgentConfig.from_settings(settings, require_llm=llm_provider is None)
    except AgentConfigError:
        logger.warning("agent disabled: invalid configuration")
        return None, None

    if llm_provider is None:
        llm_provider = OpenAICompatibleProvider(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            timeout_seconds=config.llm_timeout_seconds,
            max_tokens=config.llm_max_tokens,
        )

    if session_store is None:
        try:
            session_store = SQLiteSessionStore(
                config.session_db_path,
                max_context_messages=config.context_messages,
            )
        except (OSError, ValueError):
            logger.warning("agent disabled: session store unavailable")
            return None, None

    try:
        terms = load_business_terms(config.business_terms_path)
    except BusinessTermsError:
        # 术语文件结构错误时退回内置默认值；防虚构约束仍然生效。
        logger.warning("business terms invalid; using built-in defaults")
        terms = default_business_terms()

    tools = ToolRegistry(
        registry=registry,
        query_service=query_service,
        tool_result_max_rows=config.tool_result_max_rows,
        max_sql_bytes=settings.query_max_sql_bytes,
    )
    runner = AgentRunner(
        provider=llm_provider,
        tools=tools,
        sessions=session_store,
        terms=terms,
        max_tool_calls=config.max_tool_calls,
        sql_auto_retry_max=config.sql_auto_retry_max,
        request_timeout_seconds=config.request_timeout_seconds,
        timezone_name=config.app_timezone,
    )
    return runner, session_store


def create_app(
    settings: Settings | None = None,
    adapter_factory: Callable[[DataSourceConfig], DataAdapter] | None = None,
    *,
    llm_provider=None,
    session_store: SQLiteSessionStore | None = None,
    query_service=None,
    agent_runner: AgentRunner | None = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    参数：
        settings: 可选配置；缺省使用默认配置，测试可传入独立实例。
        adapter_factory: 可选适配器工厂（config → DataAdapter），测试注入 Fake Adapter。
        llm_provider: 可选 LLM 替身；仅测试/显式替换使用。
        session_store: 可选会话存储替身。
        query_service: 可选查询服务替身；缺省按配置构建。
        agent_runner: 可选完整 Agent 替身；提供时不再构建。
    返回：
        已挂载健康检查、数据源、聊天路由、CORS、请求 ID 中间件与异常处理的应用。
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用关闭时依次释放 Agent、查询池与注册表/适配器资源。"""
        yield
        runner = getattr(app.state, "agent_runner", None)
        if runner is not None:
            await runner.close()
        service = getattr(app.state, "query_service", None)
        if service is not None:
            await service.close()
        registry = getattr(app.state, "registry", None)
        if registry is not None:
            await registry.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    # 将配置与注册表挂到 app.state，供路由读取，保持单一配置来源。
    app.state.settings = settings
    app.state.registry = _build_registry(settings, adapter_factory)
    # 查询服务只在数据源已配置时构建；HTTP 层不暴露直接 SQL 入口。
    app.state.query_service = (
        query_service
        if query_service is not None
        else _build_query_service(settings, app.state.registry)
    )
    if agent_runner is None:
        agent_runner, session_store = _build_agent(
            settings,
            app.state.registry,
            app.state.query_service,
            llm_provider,
            session_store,
        )
    app.state.agent_runner = agent_runner
    app.state.session_store = session_store

    # CORS 边界：仅允许明确的本地开发来源，不使用通配符，不开启凭据共享。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        """为每个请求生成请求 ID；仅 /api 响应回写 X-Request-ID，保持 /health 契约不变。"""
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(DataSourceError)
    async def datasource_error_handler(request: Request, exc: DataSourceError) -> JSONResponse:
        """把领域异常转为脱敏错误信封；日志只记录请求 ID 与失败分类，不记录底层异常。"""
        logger.warning(
            "request failed request_id=%s code=%s http_status=%s",
            request.state.request_id,
            exc.code,
            exc.http_status,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(request.state.request_id, exc),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求体/路径参数结构错误也使用统一错误信封，不返回框架默认详情。

        框架默认 422 详情可能包含字段类型与输入片段；这里只返回固定 400 文案。
        """
        logger.warning(
            "request validation failed request_id=%s error_count=%s",
            request.state.request_id,
            len(exc.errors()),
        )
        return JSONResponse(
            status_code=InvalidRequest.http_status,
            content=error_envelope(request.state.request_id, InvalidRequest()),
        )

    app.include_router(health_router)
    app.include_router(datasources_router)
    app.include_router(chat_router)
    return app


# 供 `uvicorn app.main:app` 直接启动；默认配置，无外部副作用。
app = create_app()
