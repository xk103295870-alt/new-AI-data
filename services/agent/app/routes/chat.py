"""Chat / Session API：浏览器唯一的分析入口与会话恢复入口。

契约：
- POST /api/v1/chat：{session_id|null, source_id, message} → 文字结论 + 查询元数据；
- GET /api/v1/sessions/{id}：恢复基础聊天历史（P1 历史列表不在本期）。

安全边界：
- source_id 先经 DataSourceRegistry 校验（存在、已配置、授权范围非空），
  再交给 AgentRunner；不从 LLM 输出或请求中的其他字段推导；
- 错误统一走脱敏错误信封；响应不包含 SQL、密钥、连接信息或原始结果行；
  charts 由服务端从最近一次真实查询结果生成，模型只能建议展示方式。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.chart.models import ChartSpec
from app.errors import AgentUnavailable, SessionNotFound
from app.session.store import QueryInfo, SessionMessage

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    """聊天请求；字段结构由 Pydantic 校验，取值与长度由业务层再校验。"""

    session_id: str | None = None
    source_id: str
    message: str


class QueryInfoDTO(BaseModel):
    """最小查询元数据；不包含 SQL 或结果行。"""

    row_count: int
    truncated: bool
    source_id: str


class AnswerNoteDTO(BaseModel):
    """服务端确定性展示注记：稳定 code + 安全字符串参数。

    前端按当前界面语言映射；不携带模型原文或 HTML。
    """

    code: str
    params: dict[str, str] = {}


class ChatResponse(BaseModel):
    """聊天成功响应；status=completed 表示得到最终文字结论。"""

    request_id: str
    session_id: str
    status: Literal["completed"]
    answer: str
    query_info: QueryInfoDTO | None = None
    warnings: list[str] = []
    charts: list[ChartSpec] = []
    # answer_base 是去掉确定性注记后的模型原回答；answer_code/answer_params
    # 标识整段由服务端生成的固定回答。两者只用于展示本地化，不改变 answer。
    answer_base: str | None = None
    answer_code: str | None = None
    answer_params: dict[str, str] = {}
    answer_notes: list[AnswerNoteDTO] = []


class SessionMessageDTO(BaseModel):
    """会话中的单条消息。"""

    role: Literal["user", "assistant"]
    content: str
    created_at: str
    query_info: QueryInfoDTO | None = None


class SessionData(BaseModel):
    """会话快照；messages 为最近上下文窗口。"""

    session_id: str
    source_id: str
    created_at: str
    updated_at: str
    messages: list[SessionMessageDTO]


class SessionResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: SessionData


def _query_info_dto(info: QueryInfo | None) -> QueryInfoDTO | None:
    if info is None:
        return None
    return QueryInfoDTO(
        row_count=info.row_count,
        truncated=info.truncated,
        source_id=info.source_id,
    )


def _message_dto(message: SessionMessage) -> SessionMessageDTO:
    return SessionMessageDTO(
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        query_info=_query_info_dto(message.query_info),
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """执行一次自然语言分析；source_id 必须来自服务端已授权的数据源。"""
    registry = request.app.state.registry
    registry.get(payload.source_id)         # 未知 ID → 404
    registry.require_configured()           # 未启用/未配置 → 503
    registry.require_scope()                # 空/非法授权范围 → 403

    runner = getattr(request.app.state, "agent_runner", None)
    if runner is None:
        raise AgentUnavailable()

    result = await runner.run(
        session_id=payload.session_id,
        source_id=payload.source_id,
        message=payload.message,
        request_id=request.state.request_id,
    )
    return ChatResponse(
        request_id=request.state.request_id,
        session_id=result.session_id,
        status="completed",
        answer=result.answer,
        query_info=_query_info_dto(result.query_info),
        warnings=list(result.warnings),
        charts=list(result.charts),
        answer_base=result.answer_base,
        answer_code=result.answer_code,
        answer_params=dict(result.answer_params or {}),
        answer_notes=[
            AnswerNoteDTO(code=note.code, params=dict(note.params))
            for note in result.answer_notes
        ],
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request) -> SessionResponse:
    """恢复会话历史。

    本期为单租户应用：访问检查限定为“ID 格式合法且会话存在”，
    未知与非法 ID 统一 404，不泄露会话是否存在。身份系统接入后需在此
    增加主体归属校验。
    """
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise AgentUnavailable()
    session = await store.get(session_id)
    if session is None:
        raise SessionNotFound()
    return SessionResponse(
        request_id=request.state.request_id,
        status="completed",
        data=SessionData(
            session_id=session.id,
            source_id=session.source_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            messages=[_message_dto(message) for message in session.messages],
        ),
    )
