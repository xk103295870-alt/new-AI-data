"""相对日期 SQL 核验：只认可实际查询中可证明的半开区间。

职责：
- 在 QueryService 执行前，用 SQLGlot AST 检查本次 SQL 是否真的按服务端
  建议的日期字段与上下界过滤；
- 区分“服务端解析出的建议区间”和“本次 SQL 已证实的区间”。

设计原因：
- 只做字符串 contains 会把列名、注释、常量或 OR 分支误当成日期过滤；
- 模型可能遗漏 WHERE、只写单边、换错字段、把日期包进 DATE()，或者用
  OR 放宽范围，这些都可能让最终回答错误背书“已按最近 N 天统计”；
- 因此必须检查对象、字段、比较方向、上下界与半开边界。

安全边界：
- 纯 AST 检查，不执行 SQL、不访问数据库、不改写查询；
- 无法证明时返回 verified=False，调用方必须标记未验证并停止确定性区间背书；
- 不扩大 QueryPolicy 白名单，也不提供任何绕过 QueryService 的执行路径。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Union

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from app.agent.time_rules import RelativeDateRule
from app.schema.models import SchemaSummary

# 测试可直接注入 {对象名: {字段名: 类型}}；生产路径使用真实 SchemaSummary。
SchemaLike = Union[SchemaSummary, Mapping[str, Union[Mapping[str, str], Iterable[str]]]]

# MySQL 日期/时间字段类型；只有这些列才能作为相对日期过滤目标。
_TEMPORAL_TYPE_PREFIXES = ("date", "datetime", "timestamp")


@dataclass(frozen=True)
class DateScopeVerification:
    """日期语义核验结果；reason 只用于内部诊断，不进入用户回答。"""

    verified: bool
    reason: str = ""


def _reject(reason: str) -> DateScopeVerification:
    return DateScopeVerification(verified=False, reason=reason)


def _is_temporal_type(data_type: str) -> bool:
    return str(data_type or "").strip().lower().startswith(_TEMPORAL_TYPE_PREFIXES)


def _temporal_columns(schema: SchemaLike, table_name: str) -> set[str]:
    """返回目标对象中真正的日期/时间字段名。"""
    if isinstance(schema, SchemaSummary):
        for obj in schema.objects:
            if obj.name.lower() != table_name.lower():
                continue
            return {
                column.name.lower()
                for column in obj.columns
                if _is_temporal_type(column.data_type)
            }
        return set()

    columns = schema.get(table_name)
    if columns is None:
        for name, value in schema.items():
            if str(name).lower() == table_name.lower():
                columns = value
                break
    if columns is None:
        return set()
    if isinstance(columns, Mapping):
        return {
            str(name).lower()
            for name, data_type in columns.items()
            if _is_temporal_type(data_type)
        }
    # 纯字段名集合没有类型信息，测试调用方需自行保证只放时间字段。
    return {str(name).lower() for name in columns}


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    """展开顶层 AND；OR/NOT/BETWEEN 等结构原样保留，由调用方拒绝。"""
    node = _unwrap(node)
    if isinstance(node, exp.And):
        return [*_flatten_and(node.left), *_flatten_and(node.right)]
    return [node]


def _column_name(node: exp.Expression) -> str | None:
    node = _unwrap(node)
    if isinstance(node, exp.Column):
        return node.name.lower()
    return None


def _literal_date(node: exp.Expression) -> date | None:
    """只接受 YYYY-MM-DD 或零点时间，避免把区间中段当成边界。"""
    node = _unwrap(node)
    if not isinstance(node, exp.Literal) or not node.is_string:
        return None
    text = str(node.this).strip()
    if len(text) == 10:
        candidate = text
    elif len(text) >= 19 and text[10] in {" ", "T"}:
        if text[11:] not in {"00:00:00", "000000"}:
            return None
        candidate = text[:10]
    else:
        return None
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def _date_predicate(
    node: exp.Expression, temporal: set[str]
) -> tuple[str, str, date] | None:
    """识别单条 `temporal_field >= literal` 形式；其余形式不认。"""
    node = _unwrap(node)
    if isinstance(node, (exp.Or, exp.Not, exp.Between, exp.In, exp.Like)):
        return None
    if not isinstance(node, (exp.GTE, exp.GT, exp.LTE, exp.LT)):
        return None

    left_col = _column_name(node.left)
    right_col = _column_name(node.right)
    left_date = _literal_date(node.left)
    right_date = _literal_date(node.right)
    if left_col is not None and left_col in temporal and right_date is not None:
        return left_col, _operator_name(node), right_date
    if right_col is not None and right_col in temporal and left_date is not None:
        # 字面量在左侧时翻转比较方向：start <= field 等价于 field >= start。
        return right_col, _flip_operator(_operator_name(node)), left_date
    return None


def _operator_name(node: exp.Expression) -> str:
    if isinstance(node, exp.GTE):
        return ">="
    if isinstance(node, exp.GT):
        return ">"
    if isinstance(node, exp.LTE):
        return "<="
    return "<"


def _flip_operator(operator: str) -> str:
    return {">=": "<=", ">": "<", "<=": ">=", "<": ">"}[operator]


def _mentions_temporal_field(
    node: exp.Expression, temporal: set[str]
) -> bool:
    """检查条件是否以非标准形式引用了时间字段。"""
    for column in node.find_all(exp.Column):
        if column.name.lower() in temporal:
            return True
    return False


def verify_relative_date_sql(
    sql: str, rule: RelativeDateRule, schema: SchemaLike
) -> DateScopeVerification:
    """验证 SQL 是否实际使用了 rule 的日期字段、方向与半开上下界。

    返回 verified=True 仅当：
    - 单表 SELECT，WHERE 顶层只由 AND 组成；
    - 没有任何 OR/NOT/BETWEEN 或其他时间字段表达式；
    - 恰好一条 `field >= start` 与一条 `field < end`，字段是目标对象中
      真实的日期/时间列，且起止日期与 rule 完全一致。
    """
    try:
        statement = parse_one(sql, read="mysql")
    except (ParseError, ValueError):
        return _reject("parse_error")
    if not isinstance(statement, exp.Select):
        return _reject("not_select")

    table = statement.find(exp.Table)
    if table is None:
        return _reject("no_table")
    temporal = _temporal_columns(schema, table.name)
    if not temporal:
        return _reject("no_temporal_columns")

    where = statement.args.get("where")
    if where is None:
        return _reject("no_where")

    # 任何 OR/NOT/BETWEEN 都会改变布尔语义，直接拒绝，避免放宽区间。
    if where.find(exp.Or) or where.find(exp.Not) or where.find(exp.Between):
        return _reject("boolean_escape")

    lower: list[tuple[str, date]] = []
    upper: list[tuple[str, date]] = []
    for conjunct in _flatten_and(where.this):
        predicate = _date_predicate(conjunct, temporal)
        if predicate is None:
            # 条件没有引用时间字段时可以共存（如门店过滤）；
            # 一旦以 DATE(field) = ... 等非常规形式引用时间字段就拒绝。
            if _mentions_temporal_field(conjunct, temporal):
                return _reject("non_standard_temporal_predicate")
            continue
        field, operator, value = predicate
        if operator == ">=":
            lower.append((field, value))
        elif operator == "<":
            upper.append((field, value))
        else:
            # > / <= 不是半开区间的标准边界，可能漏掉或多算一天。
            return _reject("non_half_open_operator")

    if len(lower) != 1 or len(upper) != 1:
        return _reject("missing_bound")
    lower_field, lower_value = lower[0]
    upper_field, upper_value = upper[0]
    if lower_field != upper_field:
        return _reject("mixed_fields")
    if lower_value != rule.start or upper_value != rule.end_exclusive:
        return _reject("interval_mismatch")
    return DateScopeVerification(verified=True)
