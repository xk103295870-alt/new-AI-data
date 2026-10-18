"""ChartSpec 与模型建议的 Pydantic 契约。

设计原因：
- 图表数据必须来自服务端真实查询结果，模型只能建议展示方式；模型建议
  以独立通道传入，结构上不包含 dataset 字段。
- Pydantic 校验是后端最后一道边界：type/encoding 字段引用、数值类型、
  NaN/Infinity、行数与危险字段名都在这里拒绝。

注意：ChartSpec 不是 ECharts Option；前端只按已知枚举做受控映射。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

ChartType = Literal["line", "bar", "pie", "table", "metric"]

# 与查询回传/图表展示上限保持一致；超出部分由格式化器截断，并通过
# source_row_count/truncated 明确告知前端“图表只画了部分行”。
MAX_CHART_ROWS = 200
# JavaScript Number 的安全整数边界；与 query.models 的无损序列化规则一致。
MAX_SAFE_INTEGER = 2**53 - 1
MAX_TITLE_CHARS = 80
MAX_UNIT_CHARS = 16
MAX_FIELD_NAME_CHARS = 128

_CHART_TYPES = frozenset({"line", "bar", "pie", "table", "metric"})
_ENCODED_TYPES = frozenset({"line", "bar", "pie"})
_ENCODING_KEYS = frozenset({"x", "y"})
# 服务端确定性标题的稳定 code；模型给出的自由标题不带 code，保持原文。
# 前端只按这些 code 本地化，绝不替换中文标题字符。
CHART_TITLE_CODES = frozenset({"metric", "trend", "compare", "share", "table"})
_TITLE_PARAM_KEYS = {
    "metric": frozenset({"y"}),
    "trend": frozenset({"x", "y"}),
    "compare": frozenset({"x", "y"}),
    "share": frozenset({"x", "y"}),
    "table": frozenset(),
}
_SUGGESTION_KEYS = frozenset({"type", "title", "unit", "encoding"})
# 前端会把这些键当作对象属性处理；拒绝以杜绝原型污染类风险。
_DANGEROUS_FIELDS = frozenset({"__proto__", "constructor", "prototype"})
_CHART_ID_RE = re.compile(r"^chart_[0-9]{3}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# 业务词典与 Schema 注释均未确认金额币种；模型建议中的货币单位一律丢弃。
# 这里只做展示层清洗，不能替代人工复核文本结论。
_CURRENCY_UNIT_MARKERS = (
    "人民币",
    "美元",
    "欧元",
    "日元",
    "港币",
    "cny",
    "rmb",
    "usd",
    "eur",
    "jpy",
    "hkd",
    "¥",
    "￥",
    "$",
    "€",
    "£",
    "元",
)


def is_currency_unit(unit: str) -> bool:
    """判断建议单位是否在断言未确认的货币单位。"""
    text = unit.strip().lower()
    return any(marker.lower() in text for marker in _CURRENCY_UNIT_MARKERS)


class _InvalidSpec(ValueError):
    """内部校验失败；对外由 Pydantic ValidationError 统一表达。"""


def _valid_field_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_FIELD_NAME_CHARS
        and not _CONTROL_CHARS_RE.search(value)
        and value not in _DANGEROUS_FIELDS
    )


def _is_safe_number(value: Any) -> bool:
    """数值必须在 JS 安全范围内；超出后 JSON 数字会被静默改写。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= MAX_SAFE_INTEGER
    if not isinstance(value, float) or not math.isfinite(value):
        return False
    # 大浮点已是整数形式时，JS 无法安全进行整数运算或精确展示。
    return not (value.is_integer() and abs(value) > MAX_SAFE_INTEGER)


def _is_scalar(value: Any) -> bool:
    """dataset 单元格：字符串/布尔/null 或 JS 安全数值。

    超界数值由格式化器转为字符串后再进入 dataset，保证前端不会收到
    一个已被静默改写的 JSON 数字。
    """
    if value is None or isinstance(value, (str, bool)):
        return True
    return _is_safe_number(value)


def _is_finite_number(value: Any) -> bool:
    """图表指标只能是安全有限数值；bool 是 int 子类，必须显式排除。"""
    return _is_safe_number(value)


def _is_measure(value: Any) -> bool:
    # None 表示该点缺失，前端会渲染为断点；其余必须是安全有限数值。
    return value is None or _is_finite_number(value)


def _clean_text(value: str, *, max_chars: int) -> str:
    text = value.strip()
    if not text or len(text) > max_chars or _CONTROL_CHARS_RE.search(text):
        raise _InvalidSpec("text is empty, too long, or contains control characters")
    return text


class ChartSpec(BaseModel):
    """一张图的完整语义描述；dataset 必须由真实查询结果构造。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    type: ChartType
    title: str
    # 确定性标题的展示元数据；title_code 为空表示标题来自模型，必须保持原文。
    title_code: str | None = None
    title_params: dict[str, str] = Field(default_factory=dict)
    dataset: list[dict[str, Any]]
    encoding: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    # 图表数据只保留前 N 行；这两个字段区分“查询截断”和“图表裁剪”，
    # query_info 仍保持查询语义，不能被图表裁剪覆盖。
    source_row_count: StrictInt
    truncated: StrictBool

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _clean_text(value, max_chars=MAX_TITLE_CHARS)

    @field_validator("unit")
    @classmethod
    def _validate_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_text(value, max_chars=MAX_UNIT_CHARS)

    @model_validator(mode="after")
    def _validate_contract(self) -> "ChartSpec":
        if not _CHART_ID_RE.fullmatch(self.id):
            raise _InvalidSpec("invalid chart id")
        # 标题展示元数据必须封闭：未知 code、额外参数或模型标题携带 code
        # 都拒绝，避免前端把任意字符串当翻译 key 或展示字段。
        if self.title_code is not None:
            if self.title_code not in CHART_TITLE_CODES:
                raise _InvalidSpec("invalid title code")
            expected_keys = _TITLE_PARAM_KEYS[self.title_code]
            if set(self.title_params) != expected_keys:
                raise _InvalidSpec("title params do not match title code")
            for value in self.title_params.values():
                if not _valid_field_name(value):
                    raise _InvalidSpec("invalid title param")
        elif self.title_params:
            raise _InvalidSpec("title params require a title code")
        if self.source_row_count < 0 or self.source_row_count < len(self.dataset):
            raise _InvalidSpec("source_row_count must cover the dataset")
        if self.truncated != (self.source_row_count > len(self.dataset)):
            raise _InvalidSpec("truncated must reflect chart dataset trimming")
        if len(self.dataset) > MAX_CHART_ROWS:
            raise _InvalidSpec("dataset exceeds chart row limit")
        for row in self.dataset:
            if not row:
                raise _InvalidSpec("dataset row must not be empty")
            for name, value in row.items():
                if not _valid_field_name(name):
                    raise _InvalidSpec("invalid dataset field name")
                if not _is_scalar(value):
                    raise _InvalidSpec("dataset value must be a finite JSON scalar")

        for key, field in self.encoding.items():
            if key not in _ENCODING_KEYS or not _valid_field_name(field):
                raise _InvalidSpec("invalid encoding")

        if self.type == "table":
            # 表格直接展示原始列，不需要维度/指标映射，避免前端猜测语义。
            if self.encoding:
                raise _InvalidSpec("table must not carry encoding")
            return self

        if self.type == "metric":
            if set(self.encoding) != {"y"} or len(self.dataset) != 1:
                raise _InvalidSpec("metric requires one row and a y field")
            y = self.encoding["y"]
            if y not in self.dataset[0] or not _is_finite_number(self.dataset[0][y]):
                raise _InvalidSpec("metric y must be a finite number")
            return self

        # line / bar / pie
        if set(self.encoding) != {"x", "y"} or not self.dataset:
            raise _InvalidSpec("chart requires non-empty dataset and x/y encoding")
        x, y = self.encoding["x"], self.encoding["y"]
        for row in self.dataset:
            if x not in row or y not in row:
                raise _InvalidSpec("encoding must reference dataset fields")
            if not _is_scalar(row[x]) or not _is_measure(row[y]):
                raise _InvalidSpec("invalid chart point")
        return self


@dataclass(frozen=True)
class ChartSuggestion:
    """模型提出的图表建议；不含数据行，非法结构整体作废。"""

    type: ChartType | None = None
    title: str | None = None
    unit: str | None = None
    encoding: dict[str, str] | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> "ChartSuggestion | None":
        """从不可信的工具参数解析建议；任何结构问题都返回 None（回退）。"""
        if not isinstance(raw, Mapping) or not raw:
            return None
        if set(raw) - _SUGGESTION_KEYS:
            return None

        chart_type = raw.get("type")
        if chart_type is not None:
            if not isinstance(chart_type, str) or chart_type not in _CHART_TYPES:
                return None

        title = raw.get("title")
        if title is not None:
            if not isinstance(title, str):
                return None
            try:
                title = _clean_text(title, max_chars=MAX_TITLE_CHARS)
            except _InvalidSpec:
                return None

        unit = raw.get("unit")
        if unit is not None:
            if not isinstance(unit, str):
                return None
            try:
                unit = _clean_text(unit, max_chars=MAX_UNIT_CHARS)
            except _InvalidSpec:
                return None
            # 未确认币种不得进入结构化图表；非货币单位（如“笔”）保留。
            if is_currency_unit(unit):
                unit = None

        encoding = raw.get("encoding")
        if encoding is not None:
            if not isinstance(encoding, Mapping) or set(encoding) != _ENCODING_KEYS:
                return None
            parsed: dict[str, str] = {}
            for key, field in encoding.items():
                if not _valid_field_name(field):
                    return None
                parsed[key] = field
            encoding = parsed

        if (
            chart_type is None
            and title is None
            and unit is None
            and encoding is None
        ):
            return None
        return cls(type=chart_type, title=title, unit=unit, encoding=encoding)
