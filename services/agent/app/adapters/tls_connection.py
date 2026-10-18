"""认证前强制 TLS 的 aiomysql 连接（产品适配器与导入工具共用）。

aiomysql 默认只在服务器声明 CLIENT.SSL 时才升级 TLS，否则继续明文发送认证包。
本子类在 _request_authentication（发送任何认证材料之前）检查服务器能力，
不满足即关闭连接并抛 DataSourceTlsError，绝不降级明文。

兼容性风险：依赖 aiomysql 内部 _request_authentication/_connect/server_capabilities/
_ssl_context，版本锁定 >=0.3.2,<0.4（见 pyproject.toml）。产品适配器与导入工具的
连接入口共用本类，避免维护两套不一致的认证逻辑。
"""
from __future__ import annotations

import aiomysql
from pymysql.constants import CLIENT

from app.errors import DataSourceTlsError


class TlsEnforcingConnection(aiomysql.Connection):
    """认证前校验服务器声明 SSL 能力的连接。"""

    def _enforce_tls_capability(self) -> None:
        if self._ssl_context is not None and not (self.server_capabilities & CLIENT.SSL):
            self.close()
            raise DataSourceTlsError()

    async def _request_authentication(self) -> None:
        self._enforce_tls_capability()
        await super()._request_authentication()
