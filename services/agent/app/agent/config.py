"""Agent 运行配置：集中校验 LLM、工具循环、会话与超时上限。

安全边界：
- LLM 密钥只存放在 SecretStr 中，错误信息与 repr 不包含密钥。
- 所有上限都有合理天花板；非法配置拒绝启用 Agent，不用宽松默认值继续运行。
- 请求整体预算必须覆盖单次模型超时，避免子预算突破总预算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import urlparse

from pydantic import SecretStr

from app.agent.time_rules import TimeRuleError, resolve_timezone
from app.config import Settings

_MAX_LLM_TIMEOUT_SECONDS = 120.0
_MAX_REQUEST_TIMEOUT_SECONDS = 300.0
_MAX_LLM_TOKENS = 8192
_MAX_TOOL_CALLS = 10
_MAX_SQL_RETRIES = 3
_MAX_TOOL_RESULT_ROWS = 1000
_MAX_CONTEXT_MESSAGES = 200


class AgentConfigError(ValueError):
    """Agent 配置非法；消息固定，不回显密钥或原始配置值。"""


@dataclass(frozen=True)
class AgentConfig:
    """已校验的 Agent 资源上限与连接配置。"""

    llm_base_url: str
    llm_api_key: SecretStr
    llm_model: str
    llm_timeout_seconds: float
    llm_max_tokens: int
    request_timeout_seconds: float
    max_tool_calls: int
    sql_auto_retry_max: int
    tool_result_max_rows: int
    context_messages: int
    app_timezone: str
    session_db_path: str
    business_terms_path: str

    @property
    def llm_configured(self) -> bool:
        """三项 LLM 配置齐全才视为可用；测试可注入替身绕过该判断。"""
        return bool(
            self.llm_base_url
            and self.llm_model
            and self.llm_api_key.get_secret_value()
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        require_llm: bool = True,
    ) -> "AgentConfig":
        """从 Settings 映射并校验。

        require_llm=False 仅用于测试注入 LLMProvider 的场景；
        其余资源上限仍然必须合法。
        """
        base_url = settings.llm_base_url.strip()
        model = settings.llm_model.strip()
        api_key = settings.llm_api_key.get_secret_value().strip()

        if require_llm:
            if not base_url:
                raise AgentConfigError("LLM 地址未配置")
            if not model:
                raise AgentConfigError("LLM 模型未配置")
            if not api_key:
                raise AgentConfigError("LLM 密钥未配置")

        if base_url:
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise AgentConfigError("LLM 地址必须是 http(s) URL")

        llm_timeout = settings.llm_timeout_seconds
        request_timeout = settings.agent_request_timeout_seconds
        if not (math.isfinite(llm_timeout) and 0 < llm_timeout <= _MAX_LLM_TIMEOUT_SECONDS):
            raise AgentConfigError("LLM 超时参数超出合法范围")
        if not (
            math.isfinite(request_timeout)
            and 0 < request_timeout <= _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise AgentConfigError("分析请求超时参数超出合法范围")
        if request_timeout < llm_timeout:
            raise AgentConfigError("分析请求总预算不得小于单次模型超时")

        if not 1 <= settings.llm_max_tokens <= _MAX_LLM_TOKENS:
            raise AgentConfigError("LLM 最大 token 数超出合法范围")
        if not 1 <= settings.max_tool_calls <= _MAX_TOOL_CALLS:
            raise AgentConfigError("工具调用次数上限超出合法范围")
        if not 0 <= settings.sql_auto_retry_max <= _MAX_SQL_RETRIES:
            raise AgentConfigError("SQL 自动重试上限超出合法范围")
        if not 1 <= settings.agent_tool_result_max_rows <= _MAX_TOOL_RESULT_ROWS:
            raise AgentConfigError("回传模型的结果行数上限超出合法范围")
        if not 1 <= settings.agent_context_messages <= _MAX_CONTEXT_MESSAGES:
            raise AgentConfigError("会话上下文条数上限超出合法范围")

        # 相对日期依赖应用时区；非法值必须拒绝启用 Agent，不能静默按 UTC 解释。
        try:
            timezone_name = resolve_timezone(settings.app_timezone).key
        except TimeRuleError:
            raise AgentConfigError("应用时区非法") from None

        session_path = settings.session_db_path.strip()
        if not session_path:
            raise AgentConfigError("会话数据库路径不能为空")

        return cls(
            llm_base_url=base_url,
            llm_api_key=settings.llm_api_key,
            llm_model=model,
            llm_timeout_seconds=llm_timeout,
            llm_max_tokens=settings.llm_max_tokens,
            request_timeout_seconds=request_timeout,
            max_tool_calls=settings.max_tool_calls,
            sql_auto_retry_max=settings.sql_auto_retry_max,
            tool_result_max_rows=settings.agent_tool_result_max_rows,
            context_messages=settings.agent_context_messages,
            app_timezone=timezone_name,
            session_db_path=session_path,
            business_terms_path=settings.business_terms_path.strip(),
        )
