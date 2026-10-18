"""ToolRegistry：Agent 可调用的唯一可信工具集合。

工具：
- get_schema：返回授权对象与字段（只读 SchemaService，不泄露未授权内容）；
- execute_query：**只委托** Task C 的 QueryService.execute，绝不另建 SQL 执行路径。

安全边界：
- 工具定义不包含 source_id、连接信息或凭据；source_id 只由服务端运行参数提供；
- 参数由服务端严格校验（类型、长度、未知字段），不信任模型输出；
- 回传模型的行数受上限约束；错误只返回固定 code/message，不回显 SQL/常量/底层异常。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.agent.date_scope import verify_relative_date_sql
from app.agent.time_rules import RelativeDateRule
from app.datasource.registry import DataSourceRegistry
from app.errors import DataSourceError, DataSourceNotConfigured
from app.query.models import QueryResult
from app.schema.models import SchemaSummary

GET_SCHEMA_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_schema",
        "description": "获取当前数据源授权范围内的表/视图与字段；只返回授权列。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_names": {
                    "type": "array",
                    "description": "可选；要查看的对象名列表，省略或为空时返回全部授权对象。",
                    "items": {"type": "string", "maxLength": 128},
                    "maxItems": 20,
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

EXECUTE_QUERY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "execute_query",
        "description": (
            "执行一条只读 SELECT。只能查询授权对象；禁止 JOIN、子查询、UNION、"
            "写操作和 DDL。结果行数受服务端上限约束。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "单条 MySQL SELECT 语句。",
                    "maxLength": 8192,
                }
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}

SUGGEST_CHART_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "suggest_chart",
        "description": (
            "可选：在成功 execute_query 后，建议图表类型/标题/单位/字段映射。"
            "只描述展示方式，禁止提供数据行；服务端会校验建议并用真实查询结果"
            "生成 dataset，非法建议自动回退。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["line", "bar", "pie", "table", "metric"],
                },
                "title": {"type": "string", "maxLength": 80},
                "unit": {"type": "string", "maxLength": 16},
                "encoding": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "string", "maxLength": 128},
                        "y": {"type": "string", "maxLength": 128},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    GET_SCHEMA_TOOL,
    EXECUTE_QUERY_TOOL,
    SUGGEST_CHART_TOOL,
)

ERROR_TOOL_ARGUMENT_INVALID = "TOOL_ARGUMENT_INVALID"
ERROR_TOOL_UNKNOWN = "TOOL_UNKNOWN"

_MAX_ARGUMENTS_BYTES = 16384
_MAX_SCHEMA_OBJECTS_PER_CALL = 20
_MAX_OBJECT_NAME_BYTES = 128


class ToolArgumentError(ValueError):
    """工具参数非法；消息固定，不携带模型原文。"""


@dataclass(frozen=True)
class SchemaObjectBrief:
    """系统提示用的对象概览：只有名称/类型/注释，不含字段。"""

    name: str
    kind: str
    comment: str


@dataclass(frozen=True)
class ToolExecution:
    """一次工具执行结果。

    content 是回填给模型的 JSON 字符串；error_code 供 AgentRunner 决定
    是否允许有限重试或立即终止；query_result 仅用于生成本轮元数据与图表，
    不持久化原始行；chart_suggestion 是模型建议原文，必须由服务端校验。
    """

    content: str
    error_code: str | None = None
    query_result: QueryResult | None = None
    chart_suggestion: dict[str, Any] | None = None
    # 实际交给 QueryService 的 SQL 与日期范围核验结果；仅用于服务端判断
    # 最终回答能否背书相对日期区间，不持久化原始数据行。
    sql: str | None = None
    date_range_verified: bool | None = None


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _argument_error() -> ToolExecution:
    return ToolExecution(
        content=_dump(
            {
                "error": {
                    "code": ERROR_TOOL_ARGUMENT_INVALID,
                    "message": "工具参数不合法",
                    "retryable": False,
                }
            }
        ),
        error_code=ERROR_TOOL_ARGUMENT_INVALID,
    )


class ToolRegistry:
    """应用级工具注册表；按请求传入已校验的 source_id。"""

    def __init__(
        self,
        *,
        registry: DataSourceRegistry,
        query_service: Any,
        tool_result_max_rows: int = 50,
        max_sql_bytes: int = 8192,
    ) -> None:
        if tool_result_max_rows <= 0 or max_sql_bytes <= 0:
            raise ValueError("invalid tool limits")
        self._registry = registry
        self._query_service = query_service
        self._tool_result_max_rows = tool_result_max_rows
        self._max_sql_bytes = max_sql_bytes

    async def schema_overview(self, source_id: str) -> list[SchemaObjectBrief]:
        """返回授权对象概览，供系统提示做上下文选择；不含字段。

        先经注册表校验 source_id（与 chat 路由同一检查），使直接调用
        AgentRunner 的内部路径也不能读取未授权数据源的元数据。
        """
        summary = await self._load_schema(source_id)
        return [
            SchemaObjectBrief(name=obj.name, kind=obj.kind, comment=obj.comment)
            for obj in summary.objects
        ]

    async def call(
        self,
        name: str,
        arguments_json: str,
        *,
        source_id: str,
        request_id: str,
        date_rule: RelativeDateRule | None = None,
    ) -> ToolExecution:
        """执行一次工具调用。

        参数：
            name: 模型请求的工具名；
            arguments_json: 模型给出的参数 JSON 原文（不可信）；
            source_id: 服务端已校验的数据源 ID，不接受模型传入；
            request_id: 请求追踪 ID，用于审计与 QueryService 调用；
            date_rule: 本次问题由服务端解析出的相对日期规则；仅用于执行前
                核验 SQL 是否真的带有该半开区间，不改变实际执行的 SQL。
        """
        if not isinstance(arguments_json, str) or len(
            arguments_json.encode("utf-8")
        ) > _MAX_ARGUMENTS_BYTES:
            return _argument_error()
        try:
            arguments = json.loads(arguments_json)
        except (TypeError, ValueError):
            return _argument_error()
        if not isinstance(arguments, dict):
            return _argument_error()

        if name == "get_schema":
            return await self._get_schema(arguments, source_id)
        if name == "execute_query":
            return await self._execute_query(
                arguments, source_id, request_id, date_rule=date_rule
            )
        if name == "suggest_chart":
            return self._suggest_chart(arguments)
        return ToolExecution(
            content=_dump(
                {
                    "error": {
                        "code": ERROR_TOOL_UNKNOWN,
                        "message": "未知工具",
                        "retryable": False,
                    }
                }
            ),
            error_code=ERROR_TOOL_UNKNOWN,
        )

    async def _load_schema(self, source_id: str) -> SchemaSummary:
        # 注册表校验是工具层的第一道边界；未知 ID 固定 404，不泄露其他数据源。
        self._registry.get(source_id)
        service = self._registry.service
        if service is None:
            raise DataSourceNotConfigured()
        summary, _fetched_at, _warnings, _cached = await service.get_schema(source_id)
        return summary

    async def _get_schema(
        self, arguments: dict[str, Any], source_id: str
    ) -> ToolExecution:
        if set(arguments) - {"object_names"}:
            return _argument_error()
        requested_raw = arguments.get("object_names")
        requested: set[str] = set()
        if requested_raw is not None:
            if not isinstance(requested_raw, list) or len(requested_raw) > _MAX_SCHEMA_OBJECTS_PER_CALL:
                return _argument_error()
            for item in requested_raw:
                if not isinstance(item, str) or not item.strip():
                    return _argument_error()
                if len(item.encode("utf-8")) > _MAX_OBJECT_NAME_BYTES:
                    return _argument_error()
                requested.add(item)

        summary = await self._load_schema(source_id)
        objects = summary.objects
        if requested:
            objects = [obj for obj in objects if obj.name in requested]

        payload: dict[str, Any] = {
            "objects": [
                {
                    "name": obj.name,
                    "kind": obj.kind,
                    "comment": obj.comment,
                    "columns": [
                        {
                            "name": col.name,
                            "data_type": col.data_type,
                            "nullable": col.nullable,
                        }
                        for col in obj.columns
                    ],
                }
                for obj in objects
            ]
        }
        if requested and not objects:
            payload["note"] = "未找到请求的授权对象，请只使用系统提示中列出的对象。"
        return ToolExecution(content=_dump(payload))

    def _suggest_chart(self, arguments: dict[str, Any]) -> ToolExecution:
        """记录模型图表建议；不做“成功/失败”业务判断。

        建议字段是否合法、是否与真实结果匹配由 ChartSpecFormatter 校验；
        这里不读取查询结果，也不接受任何 dataset/data/rows 字段——即使模型
        传入，未知字段会在格式化阶段导致整条建议作废。
        """
        return ToolExecution(
            content=_dump({"status": "recorded"}),
            chart_suggestion=arguments,
        )

    async def _execute_query(
        self,
        arguments: dict[str, Any],
        source_id: str,
        request_id: str,
        *,
        date_rule: RelativeDateRule | None = None,
    ) -> ToolExecution:
        # 严格单字段：任何额外参数（尤其是 source_id）都拒绝，避免模型影响执行边界。
        if set(arguments) != {"sql"}:
            return _argument_error()
        sql = arguments.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return _argument_error()
        sql = sql.strip()
        if len(sql.encode("utf-8")) > self._max_sql_bytes:
            return _argument_error()

        # 在执行前完成日期语义核验：只读 Schema + SQL AST，不改变执行路径。
        # Schema 读取失败时标记未验证，但仍交给 QueryService 决定是否执行；
        # 这样不会把核验失败伪装成“已按区间统计”。
        date_range_verified: bool | None = None
        if date_rule is not None:
            try:
                summary = await self._load_schema(source_id)
                date_range_verified = verify_relative_date_sql(
                    sql, date_rule, summary
                ).verified
            except DataSourceError:
                date_range_verified = False

        try:
            result: QueryResult = await self._query_service.execute(
                source_id, sql, request_id
            )
        except DataSourceError as exc:
            # 只返回稳定 code 与固定文案；不拼接 SQL、常量或驱动信息。
            return ToolExecution(
                content=_dump(
                    {
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "retryable": exc.retryable,
                        }
                    }
                ),
                error_code=exc.code,
                sql=sql,
                date_range_verified=date_range_verified,
            )

        rows = result.rows[: self._tool_result_max_rows]
        # 查询截断（服务端结果上限）与回填截断（只把前 N 行交给模型）是
        # 两个独立事实；合并成一个布尔值会让模型把 50 行回填误说成查询上限。
        query_truncated = bool(result.truncated)
        tool_payload_truncated = len(rows) < result.row_count
        payload: dict[str, Any] = {
            "source_id": result.source_id,
            "columns": [
                {"name": col.name, "data_type": col.data_type} for col in result.columns
            ],
            "rows": rows,
            "row_count": result.row_count,
            "rows_returned": len(rows),
            "query_truncated": query_truncated,
            "tool_payload_truncated": tool_payload_truncated,
            # 兼容旧契约：truncated 继续表示查询本身是否被截断。
            "truncated": query_truncated,
        }
        if result.row_count == 0:
            payload["note"] = "没有查询到符合条件的数据，不得虚构趋势或数字。"
        elif query_truncated and tool_payload_truncated:
            payload["note"] = (
                f"服务端查询返回 {result.row_count} 行并已截断，可能还有更多行；"
                f"其中仅前 {len(rows)} 行回填给模型。图表可由服务端已返回的 "
                f"{result.row_count} 行生成，但不得称为完整结果，也不得对前 "
                f"{len(rows)} 行求和冒充完整总额。"
            )
        elif query_truncated:
            payload["note"] = (
                f"服务端查询返回 {result.row_count} 行并已截断，可能还有更多行；"
                "图表只能基于已返回行，不得称为完整结果或完整总额。"
            )
        elif tool_payload_truncated:
            payload["note"] = (
                f"查询实际返回 {result.row_count} 行，但仅前 {len(rows)} 行回填给模型；"
                "图表可由服务端完整查询结果生成。不得把回填上限说成查询返回上限，"
                f"也不得对前 {len(rows)} 行求和冒充完整总额。"
            )
        return ToolExecution(
            content=_dump(payload),
            query_result=result,
            sql=sql,
            date_range_verified=date_range_verified,
        )
