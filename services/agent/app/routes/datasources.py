"""数据源 API：列表与授权 Schema 发现。

- GET /api/v1/datasources：返回固定数据源的 ID/展示名/配置/最近检查状态，不隐式连接。
- GET /api/v1/datasources/{id}/schema：访问检查后首次加载/缓存过期时连接并查询授权元数据。
错误统一由 DataSourceError 异常处理器转为脱敏错误信封；成功响应由 Pydantic 驱动。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.datasource.registry import DatasourceItem, DataSourceRegistry
from app.schema.models import SchemaSummary
from app.schema.service import warning_codes

router = APIRouter(prefix="/api/v1/datasources", tags=["datasources"])


class DatasourcesData(BaseModel):
    datasources: list[DatasourceItem]


class DatasourcesResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: DatasourcesData


class SchemaData(BaseModel):
    source_id: str
    fetched_at: datetime
    cached: bool
    warnings: list[str]
    warning_codes: list[str] = []
    schema_summary: SchemaSummary = Field(serialization_alias="schema")


class SchemaResponse(BaseModel):
    request_id: str
    status: Literal["completed"]
    data: SchemaData


@router.get("", response_model=DatasourcesResponse)
async def list_datasources(request: Request) -> DatasourcesResponse:
    registry: DataSourceRegistry = request.app.state.registry
    return DatasourcesResponse(
        request_id=request.state.request_id,
        status="completed",
        data=DatasourcesData(datasources=[registry.list_item()]),
    )


@router.get("/{source_id}/schema", response_model=SchemaResponse)
async def get_schema(source_id: str, request: Request) -> SchemaResponse:
    registry: DataSourceRegistry = request.app.state.registry
    registry.get(source_id)         # 未知 ID → 404
    registry.require_configured()   # 未启用/未配置 → 503
    registry.require_scope()        # 空/非法授权范围 → 403
    assert registry.service is not None  # require_configured 已保证已配置

    summary, fetched_at, warnings, cached = await registry.service.get_schema(source_id)
    return SchemaResponse(
        request_id=request.state.request_id,
        status="completed",
        data=SchemaData(
            source_id=source_id,
            fetched_at=fetched_at,
            cached=cached,
            warnings=warnings,
            warning_codes=warning_codes(warnings),
            schema_summary=summary,
        ),
    )
