"""Schema DTO：对外返回的结构化元数据模型，供 OpenAPI 生成与前端校验。

主外键以有序列数组表达；对象/列仅包含授权范围内的内容。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ColumnInfo(BaseModel):
    """授权列：名称、类型、可空性与注释。"""

    name: str
    data_type: str
    nullable: bool
    comment: str


class ForeignKey(BaseModel):
    """声明外键：本端列与引用端对象/列均按声明顺序对齐。"""

    name: str
    columns: list[str]
    referenced_object: str
    referenced_columns: list[str]


class SchemaObject(BaseModel):
    """单个授权表/视图。"""

    name: str
    kind: Literal["table", "view"]
    comment: str
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKey]


class SchemaSummary(BaseModel):
    """授权元数据摘要。"""

    objects: list[SchemaObject]
