"""LLMProvider：OpenAI-compatible Chat Completions 的最小异步封装。

职责：
- 只负责一次模型调用：发送 messages/tools，解析 content 与 tool_calls；
- 不写死供应商 SDK，通过 base_url/model 适配兼容端点，测试可注入 transport；
- 统一映射超时、网络、HTTP 错误与畸形响应，文案脱敏，不回显密钥/响应体/堆栈。

安全边界：
- 密钥只保存在 SecretStr 中，绝不出现在 URL、请求体、日志或异常文案里；
- 工具调用参数有长度上限，畸形/超限响应按 MODEL_RESPONSE_INVALID 拒绝；
- 不自动重试模型请求，避免重复工具调用；重试策略由上层工具循环决定。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import httpx
from pydantic import SecretStr

from app.errors import ModelResponseInvalid, ModelTimeout, ModelUnavailable

logger = logging.getLogger(__name__)

_MAX_TOOL_CALLS_PER_RESPONSE = 10
_MAX_TOOL_NAME_BYTES = 128


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用；arguments 保留原始 JSON 字符串。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AssistantMessage:
    """一次 assistant 回复：文字内容与（可选的）工具调用。"""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMProvider(Protocol):
    """可注入的模型提供者协议；业务层只依赖 generate()。"""

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> AssistantMessage: ...


def _invalid() -> ModelResponseInvalid:
    return ModelResponseInvalid()


def parse_assistant_message(
    payload: Any,
    *,
    max_tool_argument_bytes: int = 16384,
) -> AssistantMessage:
    """严格解析 OpenAI 兼容响应；任何结构偏差都拒绝，不做猜测性修复。"""
    if not isinstance(payload, dict):
        raise _invalid()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _invalid()
    first = choices[0]
    if not isinstance(first, dict):
        raise _invalid()
    message = first.get("message")
    if not isinstance(message, dict):
        raise _invalid()

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _invalid()

    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list) or len(raw_calls) > _MAX_TOOL_CALLS_PER_RESPONSE:
        raise _invalid()

    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            raise _invalid()
        function = raw.get("function")
        if not isinstance(function, dict):
            raise _invalid()
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name.strip():
            raise _invalid()
        if len(name.encode("utf-8")) > _MAX_TOOL_NAME_BYTES:
            raise _invalid()
        if not isinstance(arguments, str):
            raise _invalid()
        if len(arguments.encode("utf-8")) > max_tool_argument_bytes:
            raise _invalid()
        raw_id = raw.get("id")
        call_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else f"call_{index}"
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

    if not calls and (content is None or not content.strip()):
        # 既没有文字也没有工具调用，视为不可用响应，避免把空回答当结论。
        raise _invalid()

    return AssistantMessage(content=content, tool_calls=tuple(calls))


def assistant_message_to_openai(message: AssistantMessage) -> dict[str, Any]:
    """把内部消息转回 OpenAI messages 结构，供工具结果回填后的下一轮调用。"""
    payload: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


class OpenAICompatibleProvider:
    """OpenAI 兼容端点的 Provider 实现。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | str,
        model: str,
        timeout_seconds: float = 30.0,
        max_tokens: int = 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed_scheme = base_url.split(":", 1)[0].lower() if base_url else ""
        if parsed_scheme not in {"http", "https"} or not base_url.split("://", 1)[-1].strip("/"):
            raise ValueError("invalid llm base url")
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key.strip():
            raise ValueError("missing llm api key")
        if not model.strip():
            raise ValueError("missing llm model")
        if not (timeout_seconds > 0) or not (max_tokens > 0):
            raise ValueError("invalid llm limits")

        self._base_url = base_url.rstrip("/")
        self._api_key = SecretStr(key)
        self._model = model.strip()
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._transport = transport

    async def generate(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        *,
        max_tool_argument_bytes: int = 16384,
    ) -> AssistantMessage:
        """发起一次 Chat Completions 调用并解析结果。

        异常：
            ModelTimeout：连接/读取超时；
            ModelUnavailable：网络、鉴权或上游 HTTP 错误；
            ModelResponseInvalid：非 JSON 或结构不符。
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "auto"

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
            ) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            logger.warning("llm request timed out")
            raise ModelTimeout() from None
        except httpx.HTTPError:
            logger.warning("llm request failed: transport error")
            raise ModelUnavailable() from None

        if response.status_code < 200 or response.status_code >= 300:
            # 只记录状态码；响应体可能包含供应商内部信息，不进入日志或异常。
            logger.warning("llm request failed status=%s", response.status_code)
            raise ModelUnavailable()

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise _invalid() from None

        return parse_assistant_message(
            payload, max_tool_argument_bytes=max_tool_argument_bytes
        )

    def __repr__(self) -> str:
        """避免调试输出意外带出密钥。"""
        return f"OpenAICompatibleProvider(model={self._model!r})"
