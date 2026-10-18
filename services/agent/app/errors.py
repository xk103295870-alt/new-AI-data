"""领域异常与脱敏错误信封。

安全边界：异常只携带固定的中文文案，不拼接主机、账号、密码、连接串或
底层异常详情。API 层据此生成稳定、脱敏的错误响应，日志也只记录分类信息。
"""
from __future__ import annotations

from typing import Any


class DataSourceError(Exception):
    """数据源访问类异常基类。

    code / http_status / retryable 是稳定的对外契约，路由异常处理器统一读取；
    message 是固定脱敏文案，子类不得在其中插入连接信息。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False
    message: str = "数据源访问失败"
    # 可选的安全结构化参数（只允许字符串）；前端按稳定 code + 参数映射本地化
    # 文案，不把任意服务端字符串当 HTML 或直接透传到英文界面。
    params: dict[str, str]

    def __init__(
        self,
        message: str | None = None,
        *,
        params: dict[str, str] | None = None,
    ) -> None:
        # 允许调用方覆盖 message，但调用方必须自行保证不含敏感信息。
        super().__init__(message or self.message)
        self.message = message or self.message
        self.params = dict(params or {})



class DataSourceNotFound(DataSourceError):
    code = "DATA_SOURCE_NOT_FOUND"
    http_status = 404
    message = "数据源不存在"


class DataSourceNotConfigured(DataSourceError):
    code = "DATA_SOURCE_NOT_CONFIGURED"
    http_status = 503
    message = "数据源未配置或未启用"


class DataSourceUnavailable(DataSourceError):
    code = "DATA_SOURCE_UNAVAILABLE"
    http_status = 503
    retryable = True
    message = "数据源暂不可用"


class DataSourceAuthError(DataSourceUnavailable):
    """认证/授权失败：不可重试（凭据或账号权限问题不会自行恢复）。"""

    retryable = False
    message = "数据源认证失败"


class DataSourceTlsError(DataSourceUnavailable):
    """TLS 握手/证书/主机名校验失败：不可重试，绝不降级明文。"""

    retryable = False
    message = "数据源 TLS 连接失败"


class DataSourceTimeout(DataSourceError):
    code = "DATA_SOURCE_TIMEOUT"
    http_status = 504
    retryable = True
    message = "数据源连接或查询超时"


class SchemaNotFound(DataSourceError):
    code = "SCHEMA_NOT_FOUND"
    http_status = 404
    message = "未找到可用的授权元数据"


class QueryNotAllowed(DataSourceError):
    code = "QUERY_NOT_ALLOWED"
    http_status = 403
    message = "该访问超出当前授权范围"


# ---- Task C：受限 SQL 执行 ----
# 所有文案固定，不携带用户 SQL、常量、列名、驱动消息或结果内容。
class QueryInvalidRequest(DataSourceError):
    """请求参数本身非法（source_id/request_id/SQL 类型等）。"""

    code = "QUERY_INVALID_REQUEST"
    http_status = 400
    message = "查询请求参数不合法"


class SqlSyntaxError(DataSourceError):
    """SQL 无法被解析；不得回显原文。"""

    code = "SQL_SYNTAX_ERROR"
    http_status = 400
    message = "SQL 语句无法解析"


class SqlNotSupported(DataSourceError):
    """语法合法但超出本期支持的受限子集（默认拒绝）。"""

    code = "SQL_NOT_SUPPORTED"
    http_status = 400
    message = "该 SQL 结构不在支持范围内"


class QueryBusy(DataSourceError):
    """连接池等待者已满或等待超时；可稍后重试。"""

    code = "QUERY_BUSY"
    http_status = 429
    retryable = True
    message = "查询服务繁忙，请稍后重试"


class QueryTimeout(DataSourceError):
    """总预算或服务端执行时限耗尽。"""

    code = "QUERY_TIMEOUT"
    http_status = 504
    retryable = True
    message = "查询超时"


class QueryResultTooLarge(DataSourceError):
    """单个单元格或整个结果超过配置上限。"""

    code = "QUERY_RESULT_TOO_LARGE"
    http_status = 413
    message = "查询结果超出大小限制"


class QueryResultUnsupported(DataSourceError):
    """结果值无法无损、确定地序列化。"""

    code = "QUERY_RESULT_UNSUPPORTED"
    http_status = 422
    message = "查询结果包含不支持的数据类型"


# ---- Task D：Agent / LLM ----
# 文案固定且脱敏：不回显密钥、供应商域名、底层堆栈、SQL 或模型原文。
class InvalidRequest(DataSourceError):
    """HTTP 请求体结构不合法（FastAPI 校验失败时统一映射）。"""

    code = "INVALID_REQUEST"
    http_status = 400
    message = "请求参数不合法"


class ChatInvalidRequest(InvalidRequest):
    """聊天请求字段非法（source_id/message/session_id 等）。"""

    code = "CHAT_INVALID_REQUEST"
    message = "聊天请求参数不合法"


class SessionNotFound(DataSourceError):
    """会话不存在或 ID 非法；不区分两者，避免探测。"""

    code = "SESSION_NOT_FOUND"
    http_status = 404
    message = "会话不存在"


class AgentUnavailable(DataSourceError):
    """Agent 未配置（缺 LLM 凭据/配置非法/查询服务不可用）。"""

    code = "AGENT_UNAVAILABLE"
    http_status = 503
    message = "分析服务暂不可用"


class AmbiguousMetric(DataSourceError):
    """问题涉及未确认口径，必须要求用户澄清后才能继续。"""

    code = "AMBIGUOUS_METRIC"
    http_status = 422
    message = "该指标口径尚未确认，请补充统计口径、时间范围或字段含义后重试"

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: str = "GENERIC",
    ) -> None:
        # reason 是稳定枚举，供前端选择对应的本地化澄清文案；不携带用户原句。
        super().__init__(message, params={"reason": reason})



class AmbiguousTimeRange(DataSourceError):
    """问题包含多个、被否定或与绝对日期冲突的时间表达。

    不做通用时间 NLP：无法唯一确定用户意图时，必须在调用模型和查询前
    固定澄清，避免把第一个匹配当成唯一正确区间。
    """

    code = "AMBIGUOUS_TIME_RANGE"
    http_status = 422
    message = "问题包含多个、被否定或与绝对日期冲突的时间表达，请明确要统计的时间范围后重试"


class ModelUnavailable(DataSourceError):
    """模型端点不可用（网络、鉴权、上游错误）；不暴露供应商细节。"""

    code = "MODEL_UNAVAILABLE"
    http_status = 503
    retryable = True
    message = "模型服务暂不可用"


class ModelTimeout(DataSourceError):
    """模型请求超时。"""

    code = "MODEL_TIMEOUT"
    http_status = 504
    retryable = True
    message = "模型响应超时"


class ModelResponseInvalid(DataSourceError):
    """模型响应不符合 OpenAI 兼容结构或工具调用畸形。"""

    code = "MODEL_RESPONSE_INVALID"
    http_status = 502
    message = "模型返回内容不合法"


class AgentTimeout(DataSourceError):
    """单次分析请求的整体时间预算耗尽。"""

    code = "AGENT_TIMEOUT"
    http_status = 504
    retryable = True
    message = "分析请求超时"


def error_envelope(request_id: str, exc: DataSourceError) -> dict[str, Any]:
    """构建 MVP 错误信封；message 固定脱敏，不暴露底层异常。"""
    return {
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            # 仅结构化、脱敏字符串；前端据此本地化，不解析异常原文。
            "params": dict(exc.params),
        },
    }
