"""AgentRunner：唯一工具调用循环入口。

流程：
  校验请求 → 加载/创建会话 → 校验未确认口径 → 读取授权对象概览 →
  系统提示 + 历史 + 用户问题 → LLM ↔ 工具循环 → 最终文字结论 → 保存一轮会话。

安全边界：
- execute_query 只经 ToolRegistry 委托 QueryService，没有第二条 SQL 执行路径；
- source_id 只来自服务端已校验参数，工具定义与模型输出都不能覆盖；
- 工具调用次数、SQL 语法自动重试次数、整体请求时间均有硬上限；
- 策略拦截（越权/不支持结构）立即停止，不自动换表或继续尝试；
- 只把授权对象概览、工具结果与问题回传模型，不发送凭据或原始敏感数据。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.agent.business_terms import (
    BusinessTerms,
    build_system_prompt,
    clarification_message,
    clarification_reason,
)
from app.agent.time_rules import (
    DEFAULT_APP_TIMEZONE,
    build_relative_answer_note,
    build_relative_unverified_note,
    detect_relative_date,
    resolve_timezone,
)
from app.agent.tools import TOOL_DEFINITIONS, ToolExecution
from app.chart.formatter import ChartSpecFormatter
from app.chart.models import ChartSpec, ChartSuggestion
from app.errors import (
    AgentTimeout,
    AmbiguousMetric,
    AmbiguousTimeRange,
    ChatInvalidRequest,
    ModelResponseInvalid,
    SessionNotFound,
)
from app.llm.provider import AssistantMessage, LLMProvider, assistant_message_to_openai
from app.query.models import QueryResult
from app.session.store import QueryInfo, Session, SessionStore

logger = logging.getLogger(__name__)

# 固定对外文案：不拼接模型原文、SQL、常量或底层异常。
ANSWER_POLICY_BLOCKED = (
    "该请求超出当前授权范围或使用了不支持的 SQL 结构，已停止执行。"
    "请调整问题或联系管理员确认授权范围。"
)
ANSWER_SQL_RETRY_EXHAUSTED = (
    "生成的 SQL 多次未通过语法校验，已停止本次分析。请补充或调整问题后重试。"
)
ANSWER_TOOL_CALL_LIMIT = "分析步骤超出上限，已停止本次分析。请缩小问题范围后重试。"
ANSWER_TOOL_ARGUMENT_INVALID = "模型返回的工具调用无法执行，已停止本次分析。"
ANSWER_QUERY_TIMEOUT = "查询超时，请缩小时间范围或统计范围后重试。"
ANSWER_QUERY_BUSY = "查询服务繁忙，请稍后重试。"
ANSWER_QUERY_TOO_LARGE = "查询结果过大，请缩小统计范围后重试。"
ANSWER_QUERY_FAILED = "查询执行失败，请稍后重试。"

# 模型直接作答、本轮没有任何成功查询时的固定标注；不拼接模型原文。
WARNING_UNVERIFIED = "UNVERIFIED"
# 命中了相对日期，但并非所有用于结论的成功查询都能证明该日期区间。
WARNING_DATE_RANGE_UNVERIFIED = "DATE_RANGE_UNVERIFIED"
UNVERIFIED_ANSWER_NOTE = "（注：本次回答未经过数据库查询验证，请以实际查询结果为准。）"

_MAX_MESSAGE_CHARS = 4000
_POLICY_ERROR_CODES = {
    "QUERY_NOT_ALLOWED",
    "SQL_NOT_SUPPORTED",
    "QUERY_INVALID_REQUEST",
}
_TOOL_ARGUMENT_ERROR_CODES = {"TOOL_ARGUMENT_INVALID", "TOOL_UNKNOWN"}


@dataclass(frozen=True)
class AnswerNote:
    """服务端确定性追加的展示注记；只含稳定 code 与安全字符串参数。

    前端按用户当前界面语言映射注记；answer 字段仍保留中文完整文本，
    供会话历史与向后兼容使用，不依赖前端字符串替换。
    """

    code: str
    params: dict[str, str]


@dataclass(frozen=True)
class AgentResult:
    """一次成功（含友好拒绝）的分析结果；错误路径以异常表达。"""

    session_id: str
    answer: str
    query_info: QueryInfo | None
    warnings: tuple[str, ...]
    tool_calls_used: int
    charts: tuple[ChartSpec, ...] = ()
    # 展示元数据：固定回答 code / 注记 code + 安全参数。模型自由回答不翻译。
    answer_base: str | None = None
    answer_code: str | None = None
    answer_params: dict[str, str] | None = None
    answer_notes: tuple[AnswerNote, ...] = ()



class AgentRunner:
    """把 LLMProvider、ToolRegistry 与 SessionStore 编排为分析闭环。"""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        tools: Any,
        sessions: SessionStore,
        terms: BusinessTerms,
        max_tool_calls: int = 5,
        sql_auto_retry_max: int = 1,
        request_timeout_seconds: float = 60.0,
        chart_formatter: ChartSpecFormatter | None = None,
        now_provider: Callable[[], datetime] | None = None,
        timezone_name: str = DEFAULT_APP_TIMEZONE,
    ) -> None:
        if max_tool_calls < 1 or sql_auto_retry_max < 0 or request_timeout_seconds <= 0:
            raise ValueError("invalid agent limits")
        self._provider = provider
        self._tools = tools
        self._sessions = sessions
        self._terms = terms
        self._max_tool_calls = max_tool_calls
        self._sql_auto_retry_max = sql_auto_retry_max
        self._request_timeout_seconds = request_timeout_seconds
        self._chart_formatter = chart_formatter or ChartSpecFormatter()
        # 服务端时钟可注入；生产默认使用每次请求时的 UTC 当前时刻，
        # 再按应用时区换算，绝不把测试日期或模型日期写死。
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._timezone_name = resolve_timezone(timezone_name).key
        self._closed = False

    async def run(
        self,
        *,
        session_id: str | None,
        source_id: str,
        message: str,
        request_id: str,
    ) -> AgentResult:
        """执行一次分析请求。

        参数：
            session_id: None 表示新建会话；否则必须是已存在且属于同一数据源的会话。
            source_id: 服务端已通过 DataSourceRegistry 校验的数据源 ID。
            message: 用户问题；长度受限，命中未确认术语时直接要求澄清。
            request_id: 请求追踪 ID，仅用于日志与 QueryService，不进入模型提示。
        """
        if not isinstance(message, str) or not message.strip():
            raise ChatInvalidRequest()
        text = message.strip()
        if len(text) > _MAX_MESSAGE_CHARS:
            raise ChatInvalidRequest()
        if not isinstance(source_id, str) or not source_id.strip():
            raise ChatInvalidRequest()

        # 未确认口径的确定性拦截：不把判断权交给模型；文案针对具体缺口。
        unconfirmed = self._terms.find_unconfirmed(text)
        if unconfirmed:
            raise AmbiguousMetric(
                clarification_message(unconfirmed),
                reason=clarification_reason(unconfirmed),
            )

        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                return await self._run(
                    session_id=session_id,
                    source_id=source_id,
                    message=text,
                    request_id=request_id,
                )
        except TimeoutError:
            raise AgentTimeout() from None

    async def _run(
        self,
        *,
        session_id: str | None,
        source_id: str,
        message: str,
        request_id: str,
    ) -> AgentResult:
        # 相对时间表达必须唯一可判定：多个、否定或与绝对日期冲突时，
        # 在调用模型和查询前固定澄清，绝不强取第一个匹配。
        now = self._now_provider()
        detection = detect_relative_date(message, now, self._timezone_name)
        if detection.ambiguous:
            raise AmbiguousTimeRange()
        relative_rule = detection.rule

        session = await self._load_session(session_id, source_id)
        overview = await self._tools.schema_overview(source_id)
        # 每个请求只取一次时钟，保证系统提示里的当前时间与区间说明一致。
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(
                    self._terms,
                    overview,
                    now=now,
                    timezone_name=self._timezone_name,
                    relative_date_rule=relative_rule,
                ),
            }
        ]
        messages.extend(
            {"role": item.role, "content": item.content}
            for item in session.messages
            if item.content
        )
        messages.append({"role": "user", "content": message})

        tool_calls_used = 0
        syntax_errors = 0
        query_info: QueryInfo | None = None
        # 每个用于结论的成功查询都必须独立证明日期区间；不能只看最后一次。
        successful_query_checks: list[bool] = []
        # 查询失败后，失败前的成功证据不能继续为最终回答背书；只有后续
        # execute_query 成功才视为失败已解决（语法重试不额外放宽预算）。
        query_failure_unresolved = False
        # 只保留最近一次 execute_query 的完整结果；图表数据从这里构造，
        # 不读取模型回复或工具回填文本中的任何数据行。
        last_result: QueryResult | None = None
        chart_suggestion: dict[str, Any] | None = None

        while True:
            assistant: AssistantMessage = await self._provider.generate(
                messages, TOOL_DEFINITIONS
            )
            if not assistant.tool_calls:
                answer = (assistant.content or "").strip()
                if not answer:
                    raise ModelResponseInvalid()
                # answer_base 保存模型原回答；确定性注记通过 answer_notes
                # 单独下发，前端按当前界面语言渲染，不翻译模型自由文本。
                answer_base = answer
                display_notes: list[AnswerNote] = []
                # 只有每次用于结论的成功查询都被服务端证明为同一半开区间时，
                # 才能追加确定性区间说明；否则只标记未验证，不输出具体日期。
                warnings: tuple[str, ...] = ()
                if relative_rule is not None:
                    # 日期背书必须同时满足：没有未解决失败、当前仍有成功查询
                    # 结果作为结论证据、且每次用于结论的成功查询都验证了区间。
                    if (
                        not query_failure_unresolved
                        and last_result is not None
                        and query_info is not None
                        and successful_query_checks
                        and all(successful_query_checks)
                    ):
                        answer = (
                            f"{answer}\n\n{build_relative_answer_note(relative_rule)}"
                        )
                        display_notes.append(
                            AnswerNote(
                                "DATE_RANGE_VERIFIED",
                                {
                                    "days": str(relative_rule.days),
                                    "timezone": relative_rule.timezone_name,
                                    "start": relative_rule.start_text,
                                    "end": relative_rule.end_text,
                                },
                            )
                        )
                    else:
                        warnings = (WARNING_DATE_RANGE_UNVERIFIED,)
                        answer = (
                            f"{answer}\n\n"
                            f"{build_relative_unverified_note(relative_rule)}"
                        )
                        display_notes.append(
                            AnswerNote(
                                "DATE_RANGE_UNVERIFIED",
                                {
                                    "days": str(relative_rule.days),
                                    "timezone": relative_rule.timezone_name,
                                },
                            )
                        )
                if query_info is None:
                    # 本轮没有任何成功查询：保留既有 UNVERIFIED 语义，
                    # 同时（如命中相对日期）保留 DATE_RANGE_UNVERIFIED。
                    warnings = (WARNING_UNVERIFIED, *warnings)
                    answer = f"{answer}\n{UNVERIFIED_ANSWER_NOTE}"
                    display_notes.append(AnswerNote("UNVERIFIED", {}))
                return await self._finish(
                    session=session,
                    user_content=message,
                    answer=answer,
                    query_info=query_info,
                    warnings=warnings,
                    tool_calls_used=tool_calls_used,
                    charts=self._charts(last_result, chart_suggestion),
                    answer_base=answer_base,
                    answer_notes=tuple(display_notes),
                )

            messages.append(assistant_message_to_openai(assistant))
            for call in assistant.tool_calls:
                if tool_calls_used >= self._max_tool_calls:
                    return await self._finish(
                        session=session,
                        user_content=message,
                        answer=ANSWER_TOOL_CALL_LIMIT,
                        query_info=query_info,
                        warnings=("TOOL_CALL_LIMIT",),
                        tool_calls_used=tool_calls_used,
                        answer_code="TOOL_CALL_LIMIT",
                    )
                tool_calls_used += 1
                execution: ToolExecution = await self._tools.call(
                    call.name,
                    call.arguments,
                    source_id=source_id,
                    request_id=request_id,
                    date_rule=relative_rule,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": execution.content,
                    }
                )

                if call.name == "execute_query":
                    query_info = _query_info(execution)
                    # 失败时 execution.query_result 为 None；不沿用更早的成功行，
                    # 避免图表与当前 query_info/回答不一致。
                    last_result = execution.query_result
                    chart_suggestion = None
                    if execution.query_result is not None:
                        # 只记录“本次 SQL 是否真的带有该日期半开区间”。
                        successful_query_checks.append(
                            execution.date_range_verified is True
                        )
                        # 新一次成功查询已重新建立当前证据，之前的失败被修正。
                        query_failure_unresolved = False
                    else:
                        # 失败本身可能被下一次成功查询修正；在修正前，
                        # 最终回答不得沿用失败前的日期区间背书。
                        query_failure_unresolved = True
                elif call.name == "suggest_chart":
                    chart_suggestion = execution.chart_suggestion

                code = execution.error_code
                if code is None:
                    continue

                # 图表建议是可选增强：参数格式错误时忽略建议，不终止分析。
                if call.name == "suggest_chart" and code in _TOOL_ARGUMENT_ERROR_CODES:
                    continue

                if code == "SQL_SYNTAX_ERROR":
                    syntax_errors += 1
                    if syntax_errors > self._sql_auto_retry_max:
                        return await self._finish(
                            session=session,
                            user_content=message,
                            answer=ANSWER_SQL_RETRY_EXHAUSTED,
                            query_info=None,
                            warnings=("SQL_RETRY_EXHAUSTED",),
                            tool_calls_used=tool_calls_used,
                            answer_code="SQL_RETRY_EXHAUSTED",
                        )
                    # 允许模型基于固定错误分类修正一次（受总预算与步数上限约束）。
                    continue

                if code in _POLICY_ERROR_CODES:
                    return await self._finish(
                        session=session,
                        user_content=message,
                        answer=ANSWER_POLICY_BLOCKED,
                        query_info=None,
                        warnings=("POLICY_BLOCKED",),
                        tool_calls_used=tool_calls_used,
                        answer_code="POLICY_BLOCKED",
                    )

                if code in _TOOL_ARGUMENT_ERROR_CODES:
                    return await self._finish(
                        session=session,
                        user_content=message,
                        answer=ANSWER_TOOL_ARGUMENT_INVALID,
                        query_info=None,
                        warnings=("TOOL_ARGUMENT_INVALID",),
                        tool_calls_used=tool_calls_used,
                        answer_code="TOOL_ARGUMENT_INVALID",
                    )

                answer, warning = _query_failure_answer(code)
                return await self._finish(
                    session=session,
                    user_content=message,
                    answer=answer,
                    query_info=None,
                    warnings=(warning,),
                    tool_calls_used=tool_calls_used,
                    answer_code=warning,
                )

    async def _load_session(self, session_id: str | None, source_id: str) -> Session:
        if session_id is None:
            return await self._sessions.create(source_id)
        if not isinstance(session_id, str) or not session_id.strip():
            raise ChatInvalidRequest()
        session = await self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound()
        if session.source_id != source_id:
            # 会话绑定创建时的数据源，禁止跨源续聊。
            raise ChatInvalidRequest()
        return session

    def _charts(
        self,
        result: QueryResult | None,
        suggestion_raw: dict[str, Any] | None,
    ) -> tuple[ChartSpec, ...]:
        """从最近一次真实查询结果生成至多一张图；建议非法时内部回退。"""
        if result is None:
            return ()
        suggestion = (
            ChartSuggestion.from_mapping(suggestion_raw)
            if suggestion_raw is not None
            else None
        )
        chart = self._chart_formatter.format(result, suggestion=suggestion)
        return (chart,) if chart is not None else ()

    async def _finish(
        self,
        *,
        session: Session,
        user_content: str,
        answer: str,
        query_info: QueryInfo | None,
        warnings: tuple[str, ...],
        tool_calls_used: int,
        charts: tuple[ChartSpec, ...] = (),
        answer_base: str | None = None,
        answer_code: str | None = None,
        answer_params: dict[str, str] | None = None,
        answer_notes: tuple[AnswerNote, ...] = (),
    ) -> AgentResult:
        base = answer_base if answer_base is not None else answer
        notes = list(answer_notes)
        # 只有“正常回答 + 本轮无任何成功查询”才标注，失败路径已有各自告警码，不叠加。
        if query_info is None and not warnings:
            warnings = (WARNING_UNVERIFIED,)
            answer = f"{answer}\n{UNVERIFIED_ANSWER_NOTE}"
            notes.append(AnswerNote("UNVERIFIED", {}))
        await self._sessions.append_turn(
            session.id,
            user_content=user_content,
            answer=answer,
            query_info=query_info,
        )
        self._log_result(session.id, tool_calls_used, warnings)
        return AgentResult(
            session_id=session.id,
            answer=answer,
            query_info=query_info,
            warnings=warnings,
            tool_calls_used=tool_calls_used,
            charts=charts,
            answer_base=base,
            answer_code=answer_code,
            answer_params=dict(answer_params or {}),
            answer_notes=tuple(notes),
        )

    def _log_result(
        self, session_id: str, tool_calls_used: int, warnings: tuple[str, ...]
    ) -> None:
        """只记录非敏感元数据；不记录问题、回答、SQL 或结果行。"""
        logger.info(
            "agent run completed session=%s tool_calls=%s warnings=%s",
            session_id,
            tool_calls_used,
            ",".join(warnings) or "-",
        )

    async def close(self) -> None:
        """幂等释放 Provider 资源；SessionStore 由应用统一管理。"""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._provider, "close", None)
        if close is not None:
            await close()


def _query_info(execution: ToolExecution) -> QueryInfo | None:
    """从成功的查询结果提取最小元数据；失败时返回 None，避免沿用旧结果。"""
    result = execution.query_result
    if result is None:
        return None
    return QueryInfo(
        row_count=result.row_count,
        truncated=result.truncated,
        source_id=result.source_id,
    )


def _query_failure_answer(code: str) -> tuple[str, str]:
    """把查询失败分类映射为固定友好文案与告警码。"""
    if code == "QUERY_TIMEOUT":
        return ANSWER_QUERY_TIMEOUT, "QUERY_TIMEOUT"
    if code == "QUERY_BUSY":
        return ANSWER_QUERY_BUSY, "QUERY_BUSY"
    if code == "QUERY_RESULT_TOO_LARGE":
        return ANSWER_QUERY_TOO_LARGE, "QUERY_RESULT_TOO_LARGE"
    return ANSWER_QUERY_FAILED, "QUERY_FAILED"
