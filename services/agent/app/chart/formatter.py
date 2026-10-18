"""ChartSpecFormatter：从真实 QueryResult 确定性推导可渲染的图表语义。

规则（无模型建议时的默认）：
- 单行单数值列 → metric；
- 时间/日期维度 + 单个数值指标 → line；
- 类别维度 + 单个数值指标 → bar；
- 多列/无法归约/空结果 → table。

模型建议只能覆盖 type/title/unit/encoding，且必须与真实列类型和 dataset
字段一致；任何非法建议整体回退到确定性结果，模型无法注入数据行。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.chart.models import (
    MAX_CHART_ROWS,
    MAX_SAFE_INTEGER,
    MAX_TITLE_CHARS,
    ChartSpec,
    ChartSuggestion,
    ChartType,
    _is_safe_number,
    _is_scalar,
    _valid_field_name,
    is_currency_unit,
)
from app.query.models import QueryColumn, QueryResult

_NUMERIC_TYPES = frozenset({"integer", "decimal", "float"})
_TEMPORAL_TYPES = frozenset({"date", "datetime", "time"})


class _Unplottable(ValueError):
    """某个值无法无损映射为图表数值；触发降级为表格或不出图。"""


@dataclass(frozen=True)
class _Shape:
    """列/行形状的确定性分类结果。"""

    kind: str  # metric | line | category | table
    x: str | None
    y: str | None
    numeric: tuple[str, ...]
    temporal: tuple[str, ...]
    category: tuple[str, ...]


@dataclass(frozen=True)
class _Resolved:
    """最终图表类型与字段映射。"""

    type: ChartType
    x: str | None
    y: str | None


class ChartSpecFormatter:
    """无状态格式化器；一次 format 只产生一张图。"""

    def __init__(self, max_rows: int = MAX_CHART_ROWS) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self._max_rows = min(max_rows, MAX_CHART_ROWS)

    def format(
        self,
        result: QueryResult | None,
        *,
        suggestion: ChartSuggestion | None = None,
        chart_id: str = "chart_001",
    ) -> ChartSpec | None:
        """返回 ChartSpec；无法安全映射时返回 None（前端降级为纯文字）。

        result 必须来自服务端 QueryService，行值已做 JSON 无损序列化；
        本方法不读取任何模型提供的行数据。
        """
        if result is None or not result.columns:
            return None

        names = [column.name for column in result.columns]
        if len(set(names)) != len(names) or not all(_valid_field_name(n) for n in names):
            return None

        extracted = _records(result, names, self._max_rows)
        if extracted is None:
            return None
        records, source_row_count = extracted
        # 图表裁剪是独立事实：查询可能完整返回 205 行，而图表只画前 200 行。
        chart_truncated = source_row_count > len(records)
        if not records:
            # 空结果也给出明确空态；不猜测图表类型。
            return self._spec(
                chart_id,
                "table",
                "查询结果",
                [],
                {},
                source_row_count=source_row_count,
                truncated=chart_truncated,
                title_code="table",
            )

        shape = _classify(result.columns, records)
        resolved = self._resolve(shape, suggestion)
        suggestion_applied = resolved is not None
        if resolved is None:
            # 建议非法时整体回退；不可让错误建议导致“无图”。
            resolved = _default_resolution(shape)
        elif resolved.type == "pie" and _pie_issue(records, resolved.y) is not None:
            # 负数、零总和或全空不具备占比语义。整条建议作废：
            # 保留原始行回退 bar/table，不取绝对值、不把 null 置零。
            resolved = _pie_fallback_resolution(shape, records)
            suggestion_applied = False

        title, title_code, title_params = self._title(resolved)
        unit = None
        if suggestion_applied and suggestion is not None:
            title = suggestion.title or title
            if suggestion.title:
                # 模型自由标题保持原文，不能套用确定性标题的翻译 code。
                title_code, title_params = None, {}
            # 模型建议解析层已丢弃货币单位；这里再做一次防御性清洗，
            # 防止其他调用方直接构造 ChartSuggestion 绕过解析。
            unit = suggestion.unit
            if unit is not None and is_currency_unit(unit):
                unit = None

        built = self._build(
            records,
            resolved,
            title,
            unit,
            chart_id,
            source_row_count=source_row_count,
            truncated=chart_truncated,
            title_code=title_code,
            title_params=title_params,
        )
        if built is not None:
            return built
        # 指标值无法映射（如非数值字符串）时，原始行仍适合表格展示。
        return self._table(
            records,
            title,
            unit,
            chart_id,
            source_row_count=source_row_count,
            truncated=chart_truncated,
            title_code=title_code,
            title_params=title_params,
        )

    # ---- 建议解析 ----

    def _resolve(
        self, shape: _Shape, suggestion: ChartSuggestion | None
    ) -> _Resolved | None:
        default = _default_resolution(shape)
        if suggestion is None:
            return default

        encoding = suggestion.encoding or {}
        x = encoding.get("x")
        y = encoding.get("y")
        all_names = shape.numeric + shape.temporal + shape.category
        if x is not None and x not in all_names:
            return None
        if y is not None and y not in all_names:
            return None

        target = suggestion.type
        if target is None:
            if shape.kind != "table":
                target = default.type
            elif x is not None and _is_temporal(x, shape):
                target = "line"
            elif x is not None and _is_category(x, shape):
                target = "bar"
            else:
                return None

        if target == "table":
            # 表格不需要维度/指标映射；带 encoding 的建议视为自相矛盾。
            return None if encoding else _Resolved("table", None, None)

        if target == "metric":
            if shape.kind != "metric" or x is not None:
                return None
            return _Resolved("metric", None, y or default.y)

        if target == "line":
            if shape.kind not in {"line", "table"}:
                return None
            xx = x or default.x
            yy = y or default.y
            if xx is None or yy is None:
                return None
            if not _is_temporal(xx, shape) or not _is_numeric(yy, shape):
                return None
            return _Resolved("line", xx, yy)

        if target in {"bar", "pie"}:
            if shape.kind not in {"category", "table"}:
                return None
            xx = x or default.x
            yy = y or default.y
            if xx is None or yy is None:
                return None
            if not _is_category(xx, shape) or not _is_numeric(yy, shape):
                return None
            return _Resolved(target, xx, yy)

        return None

    # ---- 构造 ----

    def _build(
        self,
        records: list[dict[str, Any]],
        resolved: _Resolved,
        title: str,
        unit: str | None,
        chart_id: str,
        *,
        source_row_count: int,
        truncated: bool,
        title_code: str | None = None,
        title_params: dict[str, str] | None = None,
    ) -> ChartSpec | None:
        title_params = title_params or {}
        try:
            if resolved.type == "table":
                return self._table(
                    records,
                    title,
                    unit,
                    chart_id,
                    source_row_count=source_row_count,
                    truncated=truncated,
                    title_code=title_code,
                    title_params=title_params,
                )

            if resolved.type == "metric":
                assert resolved.y is not None
                value = _coerce_measure(records[0].get(resolved.y))
                if value is None:
                    return None
                return self._spec(
                    chart_id,
                    "metric",
                    title,
                    [{resolved.y: value}],
                    {"y": resolved.y},
                    unit,
                    source_row_count=source_row_count,
                    truncated=truncated,
                    title_code=title_code,
                    title_params=title_params,
                )

            assert resolved.x is not None and resolved.y is not None
            points: list[dict[str, Any]] = []
            for record in records:
                value = _coerce_measure(record.get(resolved.y))
                points.append({resolved.x: record[resolved.x], resolved.y: value})
            return self._spec(
                chart_id,
                resolved.type,
                title,
                points,
                {"x": resolved.x, "y": resolved.y},
                unit,
                source_row_count=source_row_count,
                truncated=truncated,
                title_code=title_code,
                title_params=title_params,
            )
        except _Unplottable:
            return None

    def _table(
        self,
        records: list[dict[str, Any]],
        title: str,
        unit: str | None,
        chart_id: str,
        *,
        source_row_count: int,
        truncated: bool,
        title_code: str | None = None,
        title_params: dict[str, str] | None = None,
    ) -> ChartSpec | None:
        return self._spec(
            chart_id,
            "table",
            title,
            records,
            {},
            unit,
            source_row_count=source_row_count,
            truncated=truncated,
            title_code=title_code,
            title_params=title_params,
        )

    def _spec(
        self,
        chart_id: str,
        chart_type: ChartType,
        title: str,
        dataset: list[dict[str, Any]],
        encoding: dict[str, str],
        unit: str | None = None,
        *,
        source_row_count: int,
        truncated: bool,
        title_code: str | None = None,
        title_params: dict[str, str] | None = None,
    ) -> ChartSpec | None:
        try:
            return ChartSpec(
                id=chart_id,
                type=chart_type,
                title=title,
                title_code=title_code,
                title_params=dict(title_params or {}),
                dataset=dataset,
                encoding=encoding,
                unit=unit,
                source_row_count=source_row_count,
                truncated=truncated,
            )
        except ValidationError:
            # 例如原始行含 NaN/Infinity：宁可不展示图表，也不输出非法数据。
            return None

    @staticmethod
    def _title(resolved: _Resolved) -> tuple[str, str, dict[str, str]]:
        """返回 (中文默认标题, 稳定展示 code, 安全参数)。

        code 只用于前端本地化确定性标题；模型自由标题不会走这里。
        """
        x, y = resolved.x, resolved.y
        if resolved.type == "metric":
            text = y or "查询指标"
            return _shorten_title(text), "metric", ({"y": y} if y else {})
        elif resolved.type == "line":
            text = f"{y} 随 {x} 变化趋势"
            return _shorten_title(text), "trend", ({"x": x, "y": y} if x and y else {})
        elif resolved.type == "bar":
            text = f"{y} 按 {x} 对比"
            return _shorten_title(text), "compare", ({"x": x, "y": y} if x and y else {})
        elif resolved.type == "pie":
            text = f"{y} 按 {x} 占比"
            return _shorten_title(text), "share", ({"x": x, "y": y} if x and y else {})
        else:
            return _shorten_title("查询结果"), "table", {}


def _shorten_title(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[: MAX_TITLE_CHARS - 1] + "…"


def _records(
    result: QueryResult, names: list[str], max_rows: int
) -> tuple[list[dict[str, Any]], int] | None:
    """返回 (图表行, 查询返回行数)；结构异常或非标量值返回 None。

    第二个值用于告诉前端图表是否发生过独立裁剪，不能与 query_info 的
    查询截断混为一谈。
    """
    source_row_count = len(result.rows)
    rows = result.rows[:max_rows]
    records: list[dict[str, Any]] = []
    for row in rows:
        if len(row) != len(names):
            return None
        record: dict[str, Any] = {}
        for name, value in zip(names, row):
            # 超出 JS 安全范围的数值转成无损文本，避免 JSON 数字在浏览器
            # 被静默改写；表格可如实展示该文本，指标仍会被拒绝绘图。
            unsafe_text = _unsafe_number_text(value)
            record[name] = unsafe_text if unsafe_text is not None else value
        for value in record.values():
            if not _is_json_scalar(value):
                return None
        records.append(record)
    return records, source_row_count


def _unsafe_number_text(value: Any) -> str | None:
    """超界数值的文本表示；非数值或安全数值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if abs(value) > MAX_SAFE_INTEGER else None
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        and abs(value) > MAX_SAFE_INTEGER
    ):
        return str(value)
    return None


def _is_json_scalar(value: Any) -> bool:
    return _is_scalar(value)


def _classify(columns: list[QueryColumn], records: list[dict[str, Any]]) -> _Shape:
    numeric = tuple(c.name for c in columns if c.data_type in _NUMERIC_TYPES)
    temporal = tuple(c.name for c in columns if c.data_type in _TEMPORAL_TYPES)
    category = tuple(
        c.name
        for c in columns
        if c.data_type not in _NUMERIC_TYPES and c.data_type not in _TEMPORAL_TYPES
    )
    if len(columns) == 1 and len(numeric) == 1 and len(records) == 1:
        return _Shape("metric", None, numeric[0], numeric, temporal, category)
    if len(columns) == 2 and len(temporal) == 1 and len(numeric) == 1:
        return _Shape("line", temporal[0], numeric[0], numeric, temporal, category)
    if len(columns) == 2 and len(category) == 1 and len(numeric) == 1:
        return _Shape("category", category[0], numeric[0], numeric, temporal, category)
    return _Shape("table", None, None, numeric, temporal, category)


def _default_resolution(shape: _Shape) -> _Resolved:
    if shape.kind == "metric":
        return _Resolved("metric", None, shape.y)
    if shape.kind == "line":
        return _Resolved("line", shape.x, shape.y)
    if shape.kind == "category":
        return _Resolved("bar", shape.x, shape.y)
    return _Resolved("table", None, None)


def _pie_issue(records: list[dict[str, Any]], y: str) -> str | None:
    """返回饼图不适用原因；None 表示适合占比展示。

    适用域：所有非空指标必须 >= 0，且至少一个 > 0。null 是缺失而不是 0，
    因此允许跳过，但不能仅由 null 组成；负数不能取绝对值或置零来凑占比。
    """
    has_positive = False
    for record in records:
        try:
            value = _coerce_measure(record.get(y))
        except _Unplottable:
            return "unplottable"
        if value is None:
            continue
        if value < 0:
            return "negative"
        if value > 0:
            has_positive = True
    return None if has_positive else "no-positive"


def _pie_fallback_resolution(
    shape: _Shape, records: list[dict[str, Any]]
) -> _Resolved:
    """饼图不适用时的确定性回退：有负值的类别形状用 bar，其余用 table。"""
    if shape.kind == "category" and shape.x is not None and shape.y is not None:
        for record in records:
            try:
                value = _coerce_measure(record.get(shape.y))
            except _Unplottable:
                continue
            if value is not None and value < 0:
                return _Resolved("bar", shape.x, shape.y)
    return _Resolved("table", None, None)


def _is_numeric(name: str, shape: _Shape) -> bool:
    return name in shape.numeric


def _is_temporal(name: str, shape: _Shape) -> bool:
    return name in shape.temporal


def _is_category(name: str, shape: _Shape) -> bool:
    return name in shape.category


_MAX_SAFE_SIGNIFICANT_DIGITS = 15
_MAX_RELATIVE_ERROR = Decimal("1e-12")


def _significant_digits(value: Decimal) -> int:
    """直接在原始系数上统计有效数字，不经过上下文舍入。

    Decimal.normalize() 会先按当前上下文精度（默认 28 位）舍入，
    可能把 31 位且尾部非零的值误算成 1 位，导致超过精度门槛的值被
    错误放行绘图。这里只剥离 coefficient 的尾随零：100.00 → 1 位，
    1.00…001 → 31 位，与数学有效数字一致。
    """
    digits = value.as_tuple().digits
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
    return end


def _decimal_to_safe_float(value: Decimal) -> float:
    """把无损 Decimal 字符串转成可绘图 float，但拒绝静默失真。

    规则：
    - 精确可表示 → 接受；
    - 否则只接受有效数字 <= 15 且相对误差 <= 1e-12 的常规小数（如 0.1）；
    - 非零下溢、超出 JS 安全整数、NaN/Infinity → 拒绝，调用方降级表格。
    """
    number = float(value)
    if not math.isfinite(number):
        raise _Unplottable()
    if value != 0 and number == 0:
        # 1e-400 这类非零值不能变成 0，否则图表会展示一个错误的数据点。
        raise _Unplottable()

    exact = Decimal.from_float(number)
    if exact != value:
        if _significant_digits(value) > _MAX_SAFE_SIGNIFICANT_DIGITS:
            raise _Unplottable()
        if exact.is_zero() or abs(exact - value) / abs(value) > _MAX_RELATIVE_ERROR:
            raise _Unplottable()

    # 整数形式的大数即使可精确表示，也超出 JS 安全整数范围；
    # 前端会拒绝，后端必须保持一致。
    if number.is_integer() and abs(number) > MAX_SAFE_INTEGER:
        raise _Unplottable()
    return number


def _coerce_measure(value: Any) -> Any:
    """把 QueryResult 中的指标值转为 ECharts 可绘图数值。

    QueryResult 为无损序列化会把 Decimal 变成字符串；调用方只在数值列上
    调用本函数，因此允许有限的十进制字符串。bool、其他字符串、NaN/
    Infinity 以及无法安全数字化的值一律拒绝，由上层降级为表格。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Unplottable()
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise _Unplottable()
        return value
    if isinstance(value, float):
        if not _is_safe_number(value):
            raise _Unplottable()
        return value
    if isinstance(value, str):
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            raise _Unplottable() from None
        if not decimal_value.is_finite():
            raise _Unplottable()
        return _decimal_to_safe_float(decimal_value)
    raise _Unplottable()
