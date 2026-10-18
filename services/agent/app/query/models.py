"""查询 DTO 与无损值序列化。

安全边界：
- 结果值只允许 JSON 可无损表达的类型；Decimal 用精确字符串，超出 JS 安全
  整数范围的整数用字符串，bytes 用显式 base64 对象，拒绝 NaN/Infinity 与未知类型。
- QueryResult 不包含 SQL 原文或生成 SQL，避免结果对象被日志/响应意外带出。
"""
from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.errors import QueryResultUnsupported

# JavaScript Number 的安全整数边界；超过后转为字符串避免前端静默丢精度。
_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True)
class OutputColumn:
    """策略分析得到的输出列（名称 + 受限类型标签）。"""

    name: str
    data_type: str


@dataclass(frozen=True)
class ValidatedQuery:
    """已通过策略校验的查询；executor 只允许执行其中的生成 SQL。"""

    sql: str
    database: str
    source: str
    columns: tuple[OutputColumn, ...]
    effective_limit: int
    fetch_limit: int
    clamped: bool
    scope_fingerprint: str


class QueryColumn(BaseModel):
    """响应列描述；顺序与结果行一致。"""

    name: str
    data_type: str


class QueryResult(BaseModel):
    """受限查询结果 DTO；不包含 SQL、连接信息或原始驱动错误。"""

    request_id: str
    source_id: str
    columns: list[QueryColumn]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    duration_ms: int


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _format_timedelta(value: timedelta) -> str:
    """把 timedelta 表示为 [sign]HH:MM:SS[.ffffff]，小时不折返为天。"""
    total_us = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    sign = "-" if total_us < 0 else ""
    total_us = abs(total_us)
    hours, remainder = divmod(total_us, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, micros = divmod(remainder, 1_000_000)
    text = f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
    if micros:
        text += f".{micros:06d}"
    return text


def serialize_value(value: Any) -> tuple[Any, int]:
    """返回 (JSON 安全值, 估算字节数)；无法无损表达时抛 QueryResultUnsupported。

    字节数用于执行期的结果大小上限，宁可高估也不放宽；调用方不得跳过该检查。
    """
    if value is None:
        return None, 0

    # bool 必须在 int 之前判断（bool 是 int 的子类）。
    if isinstance(value, bool):
        return value, 4

    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            text = str(value)
            return text, _utf8_size(text)
        return value, 8

    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryResultUnsupported()
        return value, 8

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryResultUnsupported()
        # format(value, "f") 避免科学计数法，保留数据库返回的标度。
        text = format(value, "f")
        return text, _utf8_size(text)

    if isinstance(value, str):
        return value, _utf8_size(value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        encoded = base64.b64encode(raw).decode("ascii")
        return {"encoding": "base64", "data": encoded}, len(encoded)

    if isinstance(value, datetime):
        text = value.isoformat()
        return text, _utf8_size(text)

    if isinstance(value, date):
        text = value.isoformat()
        return text, _utf8_size(text)

    if isinstance(value, time):
        text = value.isoformat()
        return text, _utf8_size(text)

    if isinstance(value, timedelta):
        text = _format_timedelta(value)
        return text, _utf8_size(text)

    raise QueryResultUnsupported()


def serialize_row(values: Any) -> tuple[list[Any], int]:
    """序列化一整行并返回估算字节数；任一值失败即整次查询失败。"""
    serialized: list[Any] = []
    total = 0
    for value in values:
        converted, size = serialize_value(value)
        serialized.append(converted)
        total += size
    return serialized, total
