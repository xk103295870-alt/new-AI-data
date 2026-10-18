"""健康检查路由。

设计说明：/health 只代表进程存活，不检查数据库或模型。
“数据库连接状态”是独立的业务能力（Task B 起实现），
不能用服务存活冒充数据源已连接；前端据此区分两者。
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import Settings

router = APIRouter()


class HealthResponse(BaseModel):
    """健康检查响应契约，与 02_MVP_TECH_SPEC 保持一致。"""

    status: str
    service: str


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """返回服务存活状态；不包含配置、主机、端口或密钥。"""
    settings: Settings = request.app.state.settings
    return HealthResponse(status="ok", service=settings.app_name)
