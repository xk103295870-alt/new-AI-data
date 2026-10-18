"""数据源注册表：持有唯一预配置数据源、访问检查与最近检查状态。

本期仅支持一个服务端预配置数据源；未知 ID 统一返回 404。
访问检查：未启用/连接配置缺失 → 503；授权范围空/非法 → 403。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.datasource.config import DataSourceConfig
from app.errors import DataSourceNotConfigured, DataSourceNotFound, QueryNotAllowed
from app.schema.service import SchemaService


class DataSourceStatus:
    """最近一次真实连接检查的结果与时间；命中缓存不更新。"""

    def __init__(self) -> None:
        self.last_check_status: Literal["ok", "error"] | None = None
        self.last_check_at: datetime | None = None


class DatasourceItem(BaseModel):
    """列表端点返回的单个数据源摘要；不隐式连接数据库。"""

    id: str
    name: str
    configured: bool
    status: Literal["disabled", "pending", "ok", "error"]
    checked_at: datetime | None


class DataSourceRegistry:
    """单数据源注册表。"""

    def __init__(self, config: DataSourceConfig, service: SchemaService | None) -> None:
        self.config = config
        self.service = service
        self.status = DataSourceStatus()

    def get(self, source_id: str) -> DataSourceConfig:
        """按 ID 查找；未知 ID 统一 404，不泄露是否存在其他源。"""
        if source_id != self.config.id:
            raise DataSourceNotFound()
        return self.config

    def require_configured(self) -> None:
        """未启用或连接/数值参数非法 → 503。"""
        if not self.config.configured:
            raise DataSourceNotConfigured()

    def require_scope(self) -> None:
        """授权范围非法或为空 → 403，绝不回退成全量读取。"""
        if self.config.scope_error is not None or not self.config.scope.objects:
            raise QueryNotAllowed()

    def list_item(self) -> DatasourceItem:
        """列表状态：未配置/未启用 → disabled；否则按最近检查结果。"""
        return DatasourceItem(
            id=self.config.id,
            name=self.config.name,
            configured=self.config.configured,
            status=self._status(),
            checked_at=self.status.last_check_at,
        )

    def _status(self) -> Literal["disabled", "pending", "ok", "error"]:
        if not self.config.configured:
            return "disabled"
        if self.status.last_check_status is None:
            return "pending"
        return self.status.last_check_status

    async def close(self) -> None:
        """释放服务与适配器资源；未配置时为空操作。"""
        if self.service is not None:
            await self.service.close()
