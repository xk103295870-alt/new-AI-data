"""设置 API：LLM 自定义参数与数据源接入/倒表的管理端入口。

契约：
- GET  /api/v1/settings：LLM 与数据源的脱敏快照（密钥/密码只回布尔位）；
- PUT  /api/v1/settings/llm：保存 LLM 参数（api_key 留空=保留原密钥），保存即生效；
- POST /api/v1/settings/llm/test：以最小请求探测端点连通性与鉴权，不保存；
- PUT  /api/v1/settings/datasource：保存数据源连接与授权白名单，保存即生效；
- POST /api/v1/settings/datasource/test：以临时连接探测数据库，不保存；
- GET  /api/v1/settings/datasource/tables：倒表——发现库内全部表/视图及列，
  供管理员挑选生成授权白名单。

安全边界：
- 密钥/密码只在请求→Settings→(持久化文件/进程内) 之间流动，绝不写入响应或日志；
- 所有输入先经 Pydantic 结构校验，再经与启动配置相同的业务校验（地址/端口/上限/
  白名单），非法输入返回固定文案 400，不落盘、不触发重建；
- 保存成功后整体重建 registry → query_service → agent，旧组件异步释放；
- 测试/倒表复用领域异常映射（认证/TLS/超时/不可用），不回显驱动原文。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, SecretStr

from app.adapters.base import DataAdapter
from app.agent.config import AgentConfig, AgentConfigError
from app.config import Settings
from app.datasource.config import (
    DataSourceConfig,
    InvalidScopeError,
    build_datasource_config,
    parse_allowed_objects,
)
from app.errors import (
    DataSourceAuthError,
    DataSourceError,
    DataSourceTimeout,
    DataSourceTlsError,
    DataSourceUnavailable,
    InvalidRequest,
)
from app.llm.provider import probe_llm_endpoint

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# ---- 请求体 ----


class LlmSettingsIn(BaseModel):
    """LLM 保存请求；api_key 省略或空串 = 保留已保存密钥。"""

    base_url: str
    model: str
    timeout_seconds: float
    max_tokens: int
    temperature: float
    api_key: str | None = None


class LlmTestIn(BaseModel):
    """LLM 探测请求；全部可选，未填字段回退到已保存值。"""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class DatasourceSettingsIn(BaseModel):
    """数据源保存请求；password 省略或空串 = 保留已保存密码。"""

    enabled: bool
    name: str
    host: str
    port: int
    database: str
    user: str
    ssl_enabled: bool
    allowed_objects: dict[str, list[str]]
    password: str | None = None


class DatasourceTestIn(BaseModel):
    """数据源探测请求；可选覆盖连接参数，未填回退到已保存值。"""

    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None
    ssl_enabled: bool | None = None


# ---- 响应体 ----


class LlmSnapshot(BaseModel):
    base_url: str
    model: str
    timeout_seconds: float
    max_tokens: int
    temperature: float
    configured: bool
    api_key_set: bool


class DatasourceSnapshot(BaseModel):
    enabled: bool
    id: str
    name: str
    host: str
    port: int
    database: str
    user: str
    ssl_enabled: bool
    password_set: bool
    allowed_objects: dict[str, list[str]]


class SettingsData(BaseModel):
    llm: LlmSnapshot
    datasource: DatasourceSnapshot


class SettingsResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: SettingsData


class TestResultData(BaseModel):
    ok: bool
    latency_ms: int | None
    # 固定枚举：config-invalid / timeout / unauthorized / http-error / network /
    # invalid-response（LLM）；config-invalid / timeout / auth / tls / unavailable /
    # unknown（数据源）。成功时为 None。
    error: str | None


class TestResultResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: TestResultData


class DiscoveredColumn(BaseModel):
    name: str
    data_type: str
    nullable: bool
    comment: str


class DiscoveredObject(BaseModel):
    name: str
    kind: Literal["table", "view"]
    comment: str
    columns: list[DiscoveredColumn]


class DiscoveredObjectsData(BaseModel):
    objects: list[DiscoveredObject]
    truncated: bool


class DiscoveredObjectsResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: DiscoveredObjectsData


# ---- 校验助手 ----

_LLM_TIMEOUT_MAX = 120.0
_LLM_MAX_TOKENS_MAX = 8192


def _validate_llm_params(
    *,
    base_url: str,
    model: str,
    timeout_seconds: float,
    max_tokens: int,
    temperature: float,
) -> str | None:
    """与 AgentConfig 相同边界的业务校验；返回固定文案，非法原因不落盘。"""
    from urllib.parse import urlparse

    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "LLM 地址必须是 http(s) URL"
    if not model.strip():
        return "LLM 模型未配置"
    if not (0 < timeout_seconds <= _LLM_TIMEOUT_MAX):
        return "LLM 超时参数超出合法范围"
    if not (1 <= max_tokens <= _LLM_MAX_TOKENS_MAX):
        return "LLM 最大 token 数超出合法范围"
    if not (0.0 <= temperature <= 2.0):
        return "LLM 采样温度超出合法范围"
    return None


def _next_llm_settings(settings: Settings, body: LlmSettingsIn) -> Settings:
    """按保存请求构造新 Settings；密钥缺省=保留。校验失败抛 InvalidRequest。"""
    problem = _validate_llm_params(
        base_url=body.base_url,
        model=body.model,
        timeout_seconds=body.timeout_seconds,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
    )
    if problem:
        raise InvalidRequest(problem)

    api_key = body.api_key.strip() if body.api_key else ""
    if not api_key and not settings.llm_api_key.get_secret_value():
        raise InvalidRequest("LLM 密钥未配置")

    # AgentConfig 是全量校验入口；先用它验证组合后的配置，再落盘。
    candidate = settings.model_copy(update={
        "llm_base_url": body.base_url.strip(),
        "llm_model": body.model.strip(),
        "llm_timeout_seconds": body.timeout_seconds,
        "llm_max_tokens": body.max_tokens,
        "llm_temperature": body.temperature,
        **({"llm_api_key": SecretStr(api_key)} if api_key else {}),
    })
    try:
        AgentConfig.from_settings(candidate, require_llm=True)
    except AgentConfigError as exc:
        raise InvalidRequest(str(exc)) from None
    return candidate


def _next_datasource_settings(settings: Settings, body: DatasourceSettingsIn) -> Settings:
    """按保存请求构造新 Settings；密码缺省=保留。校验失败抛 InvalidRequest。"""
    if not body.name.strip():
        raise InvalidRequest("数据源名称不能为空")

    allowed_json = "{}"
    if body.enabled:
        # 白名单结构与边界先按运行时上限预检；build_datasource_config 再复核。
        allowed_json = json.dumps(body.allowed_objects, ensure_ascii=False)
        try:
            parse_allowed_objects(
                allowed_json,
                settings.db_metadata_max_objects,
                settings.db_metadata_max_columns,
            )
        except InvalidScopeError as exc:
            raise InvalidRequest(str(exc)) from None

    candidate = settings.model_copy(update={
        "datasource_enabled": body.enabled,
        "datasource_name": body.name.strip(),
        "db_host": body.host.strip(),
        "db_port": body.port,
        "db_name": body.database.strip(),
        "db_user": body.user.strip(),
        "db_ssl_enabled": body.ssl_enabled,
        "db_allowed_objects_json": allowed_json,
        **({"db_password": SecretStr(body.password)} if body.password else {}),
    })
    config = build_datasource_config(candidate)
    if body.enabled and config.config_error is not None:
        raise InvalidRequest(config.config_error)
    if body.enabled and config.scope_error is not None:
        raise InvalidRequest(config.scope_error)
    return candidate


def _snapshot_response(request_id: str, settings: Settings) -> SettingsResponse:
    from app.runtime_settings import datasource_snapshot, llm_snapshot

    return SettingsResponse(
        request_id=request_id,
        status="completed",
        data=SettingsData(
            llm=LlmSnapshot(**llm_snapshot(settings)),
            datasource=DatasourceSnapshot(**datasource_snapshot(settings)),
        ),
    )


def _resolve_datasource_config(
    settings: Settings,
    overrides: dict[str, Any],
) -> DataSourceConfig:
    """测试/倒表用：把可选覆盖合入 Settings 后构建配置；未启用或非法抛 400。"""
    update: dict[str, Any] = {}
    mapping = {
        "host": "db_host",
        "port": "db_port",
        "database": "db_name",
        "user": "db_user",
        "ssl_enabled": "db_ssl_enabled",
    }
    for key, field in mapping.items():
        value = overrides.get(key)
        if value is not None:
            update[field] = value
    password = overrides.get("password")
    if password:
        update["db_password"] = SecretStr(password)

    candidate = settings.model_copy(update=update) if update else settings
    candidate = candidate.model_copy(update={"datasource_enabled": True})
    config = build_datasource_config(candidate)
    if not config.configured:
        # 测试/倒表只要求连接合法；白名单为空不影响探测。
        raise InvalidRequest(config.config_error or "数据源连接配置不完整")
    return config


def _adapter_factory(request: Request) -> Any:
    """默认适配器工厂；与 main.py 的默认实现保持一致（测试可注入）。"""
    factory = getattr(request.app.state, "adapter_factory", None)
    if factory is not None:
        return factory
    from app.adapters.mysql import DirectMySQLAdapter

    return DirectMySQLAdapter


# ---- 路由 ----


@router.get("", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """LLM 与数据源的脱敏快照；密钥/密码只回布尔位。"""
    settings: Settings = request.app.state.settings
    return _snapshot_response(request.state.request_id, settings)


@router.put("/llm", response_model=SettingsResponse)
async def put_llm_settings(body: LlmSettingsIn, request: Request) -> SettingsResponse:
    """保存 LLM 参数并即时生效：持久化 → 更新 settings → 重建 Agent。"""
    settings: Settings = request.app.state.settings
    candidate = _next_llm_settings(settings, body)

    store = request.app.state.runtime_store
    store.save_llm({
        "llm_base_url": candidate.llm_base_url,
        "llm_api_key": candidate.llm_api_key,
        "llm_model": candidate.llm_model,
        "llm_timeout_seconds": candidate.llm_timeout_seconds,
        "llm_max_tokens": candidate.llm_max_tokens,
        "llm_temperature": candidate.llm_temperature,
    })
    request.app.state.settings = candidate
    await request.app.state.rebuild_runtime()
    return _snapshot_response(request.state.request_id, candidate)


@router.post("/llm/test", response_model=TestResultResponse)
async def test_llm_settings(body: LlmTestIn, request: Request) -> TestResultResponse:
    """以最小请求探测 LLM 端点；不入库、不重建、不记密钥。"""
    settings: Settings = request.app.state.settings
    base_url = (body.base_url or settings.llm_base_url).strip()
    model = (body.model or settings.llm_model).strip()
    api_key = body.api_key.strip() if body.api_key else settings.llm_api_key.get_secret_value()
    timeout_seconds = body.timeout_seconds if body.timeout_seconds is not None else min(
        settings.llm_timeout_seconds, 30.0
    )
    max_tokens = body.max_tokens or settings.llm_max_tokens
    temperature = body.temperature if body.temperature is not None else settings.llm_temperature

    problem = _validate_llm_params(
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if problem or not api_key:
        return TestResultResponse(
            request_id=request.state.request_id,
            status="completed",
            data=TestResultData(ok=False, latency_ms=None, error="config-invalid"),
        )

    ok, latency_ms, error = await probe_llm_endpoint(
        base_url=base_url,
        api_key=SecretStr(api_key),
        model=model,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
    )
    return TestResultResponse(
        request_id=request.state.request_id,
        status="completed",
        data=TestResultData(ok=ok, latency_ms=latency_ms, error=None if ok else error),
    )


@router.put("/datasource", response_model=SettingsResponse)
async def put_datasource_settings(
    body: DatasourceSettingsIn, request: Request
) -> SettingsResponse:
    """保存数据源接入信息并即时生效：持久化 → 更新 settings → 重建数据链路。"""
    settings: Settings = request.app.state.settings
    candidate = _next_datasource_settings(settings, body)

    store = request.app.state.runtime_store
    store.save_datasource({
        "datasource_enabled": candidate.datasource_enabled,
        "datasource_name": candidate.datasource_name,
        "db_host": candidate.db_host,
        "db_port": candidate.db_port,
        "db_name": candidate.db_name,
        "db_user": candidate.db_user,
        "db_password": candidate.db_password,
        "db_ssl_enabled": candidate.db_ssl_enabled,
        "db_allowed_objects_json": candidate.db_allowed_objects_json,
    })
    request.app.state.settings = candidate
    await request.app.state.rebuild_runtime()
    return _snapshot_response(request.state.request_id, candidate)


@router.post("/datasource/test", response_model=TestResultResponse)
async def test_datasource_settings(
    body: DatasourceTestIn, request: Request
) -> TestResultResponse:
    """以临时连接探测数据库（SELECT 1）；不入库、不重建。"""
    settings: Settings = request.app.state.settings
    try:
        config = _resolve_datasource_config(settings, body.model_dump(exclude_none=True))
    except InvalidRequest:
        return TestResultResponse(
            request_id=request.state.request_id,
            status="completed",
            data=TestResultData(ok=False, latency_ms=None, error="config-invalid"),
        )

    adapter_factory = _adapter_factory(request)
    adapter: DataAdapter = adapter_factory(config)
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            adapter.test_connection(),
            timeout=config.total_timeout_seconds + 1.0,
        )
    except asyncio.TimeoutError:
        return TestResultResponse(
            request_id=request.state.request_id,
            status="completed",
            data=TestResultData(ok=False, latency_ms=None, error="timeout"),
        )
    except DataSourceAuthError:
        error: str | None = "auth"
    except DataSourceTlsError:
        error = "tls"
    except DataSourceTimeout:
        error = "timeout"
    except DataSourceUnavailable:
        error = "unavailable"
    except DataSourceError:
        error = "unknown"
    except Exception:
        error = "unknown"
    else:
        error = None
    finally:
        try:
            await adapter.close()
        except Exception:
            pass

    latency_ms = int((time.perf_counter() - started) * 1000)
    return TestResultResponse(
        request_id=request.state.request_id,
        status="completed",
        data=TestResultData(ok=error is None, latency_ms=latency_ms, error=error),
    )


@router.get("/datasource/tables", response_model=DiscoveredObjectsResponse)
async def discover_datasource_tables(request: Request) -> DiscoveredObjectsResponse:
    """倒表：按已保存的数据源配置发现库内全部表/视图及列（管理员挑白名单用）。"""
    settings: Settings = request.app.state.settings
    config = _resolve_datasource_config(settings, {})

    adapter_factory = _adapter_factory(request)
    adapter: DataAdapter = adapter_factory(config)
    try:
        objects, truncated = await adapter.discover_objects()
    finally:
        try:
            await adapter.close()
        except Exception:
            pass

    return DiscoveredObjectsResponse(
        request_id=request.state.request_id,
        status="completed",
        data=DiscoveredObjectsData(
            objects=[DiscoveredObject(**obj) for obj in objects],
            truncated=truncated,
        ),
    )
