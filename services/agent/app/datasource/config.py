"""数据源配置与授权白名单解析。

安全边界：
- 连接信息只来自服务端 Settings（进程环境变量），浏览器无法传入主机/账号/密码/连接串。
- 对象与列采用精确白名单，默认空、拒绝通配符与越界，不存在“放行全库”的路径。
- 表名按数据库真实身份精确匹配（大小写敏感），列名大小写不敏感（MySQL 列名恒不敏感）。
- 密码用 SecretStr 持有，DataSourceConfig 的 repr/str 不泄露密码。
- 白名单与数值参数只在“启用数据源”时校验；禁用时不校验未来占位符，也不连接。
"""
from __future__ import annotations

import hashlib
import json
import math

from pydantic import BaseModel, SecretStr

from app.config import Settings

# 白名单不允许出现的通配字符；精确匹配是唯一授权路径。
_WILDCARDS = ("*", "%", "?")

# 参数合法上下界（B-07）：端口、超时、TTL 与元数据规模。
_PORT_MIN, _PORT_MAX = 1, 65535
_TTL_MIN, _TTL_MAX = 1, 86400
_MAX_OBJECTS_MIN, _MAX_OBJECTS_MAX = 1, 10000
_MAX_COLUMNS_MIN, _MAX_COLUMNS_MAX = 1, 100000
# 前端 Schema 请求固定 20 秒；服务端最多 15 秒，预留 5 秒用于传输等开销。
_TIMEOUT_MAX_SECONDS = 15.0


class InvalidScopeError(ValueError):
    """白名单缺失、空白、非法或越界；消息为固定安全文案，不含原始内容。"""


class AllowedScope(BaseModel):
    """对象→允许列 的精确白名单。

    对象（表/视图）名按数据库真实身份精确匹配，大小写敏感；
    列名匹配大小写不敏感（MySQL 列名恒不敏感）。指纹按语义规范化用于缓存隔离。
    """

    objects: dict[str, list[str]]

    def object_names(self) -> list[str]:
        """按名称稳定排序，保证 SQL 参数与输出顺序确定。"""
        return sorted(self.objects.keys())

    def columns_for(self, object_name: str) -> list[str]:
        """按精确对象名返回允许列；未知对象返回空列表。"""
        return list(self.objects.get(object_name, []))

    def lower_columns_for(self, object_name: str) -> list[str]:
        """某对象允许列的小写形式，供列级（大小写不敏感）过滤。"""
        return [c.lower() for c in self.columns_for(object_name)]

    def fingerprint(self) -> str:
        """授权配置的稳定指纹，用于按“数据源+授权范围”隔离缓存。

        键顺序与列顺序不影响结果（授权语义相同即同一指纹）；
        对象名大小写不同会产生不同指纹（不同授权范围）。
        """
        canonical = {name: sorted(cols) for name, cols in sorted(self.objects.items())}
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class DataSourceConfig(BaseModel):
    """从 Settings 解析出的单个数据源配置（含授权范围与校验状态）。"""

    id: str
    name: str
    enabled: bool

    # 连接参数；禁用时可为空，仅在启用时参与校验。
    host: str = ""
    port: int = 3306
    database: str = ""
    user: str = ""
    password: SecretStr = SecretStr("")
    ssl_enabled: bool = False
    ssl_ca_path: str = ""

    connect_timeout_seconds: float = 5.0
    metadata_timeout_seconds: float = 5.0
    total_timeout_seconds: float = 15.0

    scope: AllowedScope = AllowedScope(objects={})
    # 启用但白名单非法时记录安全错误文案，由请求路径返回 403。
    scope_error: str | None = None
    # 启用但连接/数值参数非法时记录安全错误文案，由请求路径返回 503。
    config_error: str | None = None

    @property
    def configured(self) -> bool:
        """启用且连接与数值参数均合法（不含白名单；白名单由 require_scope 单独校验）。"""
        return self.enabled and self.config_error is None


def parse_allowed_objects(raw: str, max_objects: int, max_columns: int) -> AllowedScope:
    """解析并校验 JSON 白名单；任何非法输入都抛 InvalidScopeError，不回退全量。"""
    if not raw or not raw.strip():
        raise InvalidScopeError("授权对象白名单为空")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise InvalidScopeError("授权对象白名单不是合法 JSON")

    if not isinstance(data, dict) or not data:
        raise InvalidScopeError("授权对象白名单必须是非空对象")

    objects: dict[str, list[str]] = {}
    seen_object_names: set[str] = set()
    total_columns = 0

    for obj, cols in data.items():
        if not isinstance(obj, str) or not obj.strip():
            raise InvalidScopeError("授权对象名必须是非空字符串")
        if any(w in obj for w in _WILDCARDS):
            raise InvalidScopeError("授权对象名不允许通配符")
        # 对象名大小写敏感（数据库可同时存在 Orders 与 orders），仅拒绝精确重复，
        # 大小写冲突由数据库语义决定，不在此处误判。
        if obj in seen_object_names:
            raise InvalidScopeError("授权对象名重复")
        seen_object_names.add(obj)

        if not isinstance(cols, list) or not cols:
            raise InvalidScopeError("授权对象的列列表必须是非空数组")

        cleaned: list[str] = []
        seen_exact: set[str] = set()
        seen_lower: set[str] = set()
        for col in cols:
            if not isinstance(col, str) or not col.strip():
                raise InvalidScopeError("授权列名必须是非空字符串")
            if any(w in col for w in _WILDCARDS):
                raise InvalidScopeError("授权列名不允许通配符")
            if col in seen_exact:
                continue  # 精确重复：去重，不影响授权语义
            if col.lower() in seen_lower:
                raise InvalidScopeError("授权列名存在大小写冲突")  # 列名大小写不敏感，冲突无意义
            seen_exact.add(col)
            seen_lower.add(col.lower())
            cleaned.append(col)

        objects[obj] = cleaned
        total_columns += len(cleaned)

    if len(objects) > max_objects:
        raise InvalidScopeError("授权对象数量超出上限")
    if total_columns > max_columns:
        raise InvalidScopeError("授权列数量超出上限")

    return AllowedScope(objects=objects)


def _validate_connection_params(settings: Settings) -> str | None:
    """在启用路径校验连接与数值参数；返回安全错误文案或 None。

    校验错误只给固定分类文案，不回显密码、连接串或完整敏感输入。
    """
    if not (settings.db_host and settings.db_user and settings.db_name):
        return "数据源连接配置不完整"

    if not (_PORT_MIN <= settings.db_port <= _PORT_MAX):
        return "数据源端口超出合法范围"

    timeouts = (
        settings.db_connect_timeout_seconds,
        settings.db_metadata_timeout_seconds,
        settings.db_total_timeout_seconds,
    )
    if not all(math.isfinite(t) and t > 0 for t in timeouts):
        return "数据源超时参数必须是有限正数"
    if any(t > _TIMEOUT_MAX_SECONDS for t in timeouts):
        return "数据源超时参数不得超过 15 秒"

    # 单阶段超时不得突破整体预算（B-08）
    if settings.db_total_timeout_seconds < settings.db_connect_timeout_seconds or \
            settings.db_total_timeout_seconds < settings.db_metadata_timeout_seconds:
        return "数据源整体超时必须不小于各阶段超时"

    if not (_TTL_MIN <= settings.schema_cache_ttl_seconds <= _TTL_MAX):
        return "数据源缓存 TTL 超出合法范围"

    if not (_MAX_OBJECTS_MIN <= settings.db_metadata_max_objects <= _MAX_OBJECTS_MAX):
        return "数据源元数据对象上限超出合法范围"
    if not (_MAX_COLUMNS_MIN <= settings.db_metadata_max_columns <= _MAX_COLUMNS_MAX):
        return "数据源元数据列上限超出合法范围"

    return None


def build_datasource_config(settings: Settings) -> DataSourceConfig:
    """从 Settings 解析数据源配置。

    禁用时不解析/校验；启用时校验连接与数值参数（写入 config_error）和白名单
    （写入 scope_error），由请求路径分别返回 503/403，不在应用启动时崩溃。
    """
    scope = AllowedScope(objects={})
    scope_error: str | None = None
    config_error: str | None = None

    if settings.datasource_enabled:
        config_error = _validate_connection_params(settings)
        if config_error is None:
            try:
                scope = parse_allowed_objects(
                    settings.db_allowed_objects_json,
                    settings.db_metadata_max_objects,
                    settings.db_metadata_max_columns,
                )
            except InvalidScopeError as exc:
                scope_error = str(exc)

    return DataSourceConfig(
        id=settings.datasource_id,
        name=settings.datasource_name,
        enabled=settings.datasource_enabled,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        ssl_enabled=settings.db_ssl_enabled,
        ssl_ca_path=settings.db_ssl_ca_path,
        connect_timeout_seconds=settings.db_connect_timeout_seconds,
        metadata_timeout_seconds=settings.db_metadata_timeout_seconds,
        total_timeout_seconds=settings.db_total_timeout_seconds,
        scope=scope,
        scope_error=scope_error,
        config_error=config_error,
    )
