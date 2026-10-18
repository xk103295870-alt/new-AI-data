"""数据适配器最小协议与原始元数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.datasource.config import AllowedScope


@dataclass
class RawMetadata:
    """information_schema 原始行（DictCursor 结果），尚未过滤或映射。

    每项均为 list[dict[str, Any]]，键为 information_schema 的大写列名；
    由 SchemaService 负责按授权范围二次过滤并映射为对外 DTO。
    """

    tables: list[dict[str, Any]] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    key_usage: list[dict[str, Any]] = field(default_factory=list)


class DataAdapter(Protocol):
    """最小数据适配器协议：连接检查、Schema 读取与资源关闭。

    刻意不含 execute_query：业务 SQL 执行能力留给 Task C，
    避免在本任务创建可绕过安全策略的任意查询入口。
    """

    async def test_connection(self) -> None:
        """执行固定连接检查（如 SELECT 1）；失败抛领域异常。"""
        ...

    async def get_schema(self, scope: AllowedScope) -> RawMetadata:
        """仅读取授权范围内的 information_schema 元数据。"""
        ...

    async def close(self) -> None:
        """释放连接/池等资源；幂等且不抛异常。"""
        ...
