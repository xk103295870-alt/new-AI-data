"""SchemaService：授权元数据发现、过滤、缓存与最近检查状态。

安全边界：
- 返回前二次过滤对象、列、主键列及外键两端，不泄露未授权对象名称；
  对象名按数据库真实身份精确匹配（大小写敏感），外键引用未授权库/对象/列时
  整条丢弃，不按字段同名推断关系。
- 不完整复合主键省略整条（不把残缺列冒充完整约束），只给脱敏告警。
- 成功结果才缓存；缓存按 (source_id, 授权配置指纹) 隔离；过期加载失败
  返回失败，不拿旧缓存冒充新结果；命中缓存不更新最近真实检查时间。
- 同源并发加载用 asyncio.Lock 合并，整体加载受 asyncio.timeout 总截止约束，
  覆盖排队、连接、查询与清理；超时取消后由适配器 finally 强制关闭连接。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from app.adapters.base import DataAdapter, RawMetadata
from app.datasource.config import AllowedScope
from app.errors import DataSourceTimeout, QueryNotAllowed, SchemaNotFound
from app.operation_budget import operation_budget
from app.schema.cache import TTLCache
from app.schema.models import ColumnInfo, ForeignKey, SchemaObject, SchemaSummary

# 过滤产生的通用告警文案；不携带任何对象/列/库名，避免泄露未授权信息。
_WARN_PARTIAL_PK = "部分主键列因授权范围未完整可见，已省略该主键"
_WARN_DROPPED_FK = "部分外键因引用未授权库、对象或列被忽略"

# 稳定告警码与文案一一对应；响应同时下发两者，前端按 code 本地化，
# 旧客户端仍可使用 warnings 文案。未知文案不猜测 code。
SCHEMA_WARNING_CODES = {
    _WARN_PARTIAL_PK: "SCHEMA_PARTIAL_PRIMARY_KEY",
    _WARN_DROPPED_FK: "SCHEMA_DROPPED_FOREIGN_KEY",
}


def warning_codes(warnings: list[str]) -> list[str]:
    """把固定告警文案映射为稳定 code；未知文案返回空，不伪造语义。"""
    return [SCHEMA_WARNING_CODES[item] for item in warnings if item in SCHEMA_WARNING_CODES]


def build_schema_summary(
    raw: RawMetadata,
    scope: AllowedScope,
    database: str,
) -> tuple[SchemaSummary, list[str]]:
    """把原始 information_schema 行映射为严格过滤后的 SchemaSummary。

    database 为当前数据源的 schema 名，用于校验外键引用的库身份（B-04）。
    无任何授权对象可见时抛 SchemaNotFound，不给成功假象。
    """
    allowed_objects = set(scope.object_names())
    warnings: list[str] = []

    # 仅保留授权对象（精确对象名），稳定输出顺序
    objects: dict[str, SchemaObject] = {}
    for row in raw.tables:
        name = row.get("TABLE_NAME", "")
        if name not in allowed_objects:
            continue
        kind = "view" if row.get("TABLE_TYPE") == "VIEW" else "table"
        objects[name] = SchemaObject(
            name=name,
            kind=kind,
            comment=row.get("TABLE_COMMENT") or "",
            columns=[],
            primary_key=[],
            foreign_keys=[],
        )

    # 约束类型：{(table_name, constraint_name): type}（精确对象名）
    constraint_types: dict[tuple[str, str], str] = {}
    for row in raw.constraints:
        constraint_types[(row.get("TABLE_NAME", ""), row.get("CONSTRAINT_NAME", ""))] = row.get(
            "CONSTRAINT_TYPE", ""
        )

    # 键列：{(table_name, constraint_name): rows}，按 ORDINAL_POSITION 稳定排序
    key_usage: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in raw.key_usage:
        key = (row.get("TABLE_NAME", ""), row.get("CONSTRAINT_NAME", ""))
        key_usage.setdefault(key, []).append(row)
    for rows in key_usage.values():
        rows.sort(key=lambda r: r.get("ORDINAL_POSITION") or 0)

    # 列：{table_name: rows}，按 ORDINAL_POSITION 排序（精确对象名）
    columns_by_table: dict[str, list[dict[str, Any]]] = {}
    for row in raw.columns:
        columns_by_table.setdefault(row.get("TABLE_NAME", ""), []).append(row)
    for rows in columns_by_table.values():
        rows.sort(key=lambda r: r.get("ORDINAL_POSITION") or 0)

    for obj_name, obj in objects.items():
        allowed_cols = {c.lower() for c in scope.columns_for(obj_name)}

        for crow in columns_by_table.get(obj_name, []):
            col_name = crow.get("COLUMN_NAME", "")
            if col_name.lower() not in allowed_cols:
                continue
            obj.columns.append(
                ColumnInfo(
                    name=col_name,
                    data_type=crow.get("DATA_TYPE", ""),
                    nullable=(crow.get("IS_NULLABLE") or "NO") == "YES",
                    comment=crow.get("COLUMN_COMMENT") or "",
                )
            )

        # 主键：只有所有列均授权才表达；否则省略整条并告警，不冒充完整约束（B-05）
        if (obj_name, "PRIMARY") in constraint_types:
            pk_rows = key_usage.get((obj_name, "PRIMARY"), [])
            ordered = [r.get("COLUMN_NAME", "") for r in pk_rows]
            if ordered and all(c.lower() in allowed_cols for c in ordered):
                obj.primary_key = ordered
            elif ordered:
                warnings.append(_WARN_PARTIAL_PK)
                obj.primary_key = []

        # 外键：引用库、目标对象与两端列都授权才保留，否则整条丢弃且不泄露目标
        fks: list[ForeignKey] = []
        for (tbl, cname), ctype in constraint_types.items():
            if tbl != obj_name or ctype != "FOREIGN KEY":
                continue
            rows = key_usage.get((tbl, cname), [])
            if not rows:
                continue
            local = [r.get("COLUMN_NAME", "") for r in rows]
            ref_schema = rows[0].get("REFERENCED_TABLE_SCHEMA") or ""
            ref_table = rows[0].get("REFERENCED_TABLE_NAME") or ""
            ref_cols = [r.get("REFERENCED_COLUMN_NAME") or "" for r in rows]
            # 引用库必须是本数据源 schema（B-04），否则整条丢弃
            if ref_schema != database:
                warnings.append(_WARN_DROPPED_FK)
                continue
            if ref_table not in allowed_objects:
                warnings.append(_WARN_DROPPED_FK)
                continue
            if not all(c.lower() in allowed_cols for c in local):
                warnings.append(_WARN_DROPPED_FK)
                continue
            ref_allowed = {c.lower() for c in scope.columns_for(ref_table)}
            if not all(c.lower() in ref_allowed for c in ref_cols):
                warnings.append(_WARN_DROPPED_FK)
                continue
            fks.append(
                ForeignKey(name=cname, columns=local, referenced_object=ref_table, referenced_columns=ref_cols)
            )
        fks.sort(key=lambda fk: fk.name)
        obj.foreign_keys = fks

    ordered = [objects[n] for n in sorted(objects.keys())]
    if not ordered:
        raise SchemaNotFound()
    return SchemaSummary(objects=ordered), warnings


class SchemaService:
    """单个数据源的元数据发现服务；持有 adapter、授权范围、缓存与检查状态。"""

    def __init__(
        self,
        adapter: DataAdapter,
        scope: AllowedScope,
        cache: TTLCache,
        status: Any,
        *,
        database: str = "",
        total_timeout_seconds: float = 15.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._scope = scope
        self._cache = cache
        self._status = status
        self._database = database
        self._total_timeout_seconds = total_timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    def _cache_key(self, source_id: str) -> tuple[str, str]:
        return (source_id, self._scope.fingerprint())

    async def get_schema(
        self, source_id: str,
    ) -> tuple[SchemaSummary, datetime, list[str], bool]:
        """返回 (summary, fetched_at, warnings, cached)。

        授权范围为空直接拒绝；命中缓存不更新最近真实检查时间；
        整体加载受总截止时间约束，超时抛 DataSourceTimeout；
        过期后加载失败抛出异常，不返回旧缓存。
        """
        if not self._scope.objects:
            raise QueryNotAllowed()

        key = self._cache_key(source_id)
        hit = self._cache.get(key)
        if hit is not None:
            summary, fetched_at, warnings = hit
            return summary, fetched_at, warnings, True

        with operation_budget(self._total_timeout_seconds) as deadline:
            return await self._load_before_deadline(key, deadline)

    async def _load_before_deadline(
        self, key: tuple[str, str], deadline: float,
    ) -> tuple[SchemaSummary, datetime, list[str], bool]:
        """锁、驱动等待及清理共享绝对截止，拒绝同步映射后已超过预算的结果。"""
        try:
            async with asyncio.timeout_at(deadline):
                async with self._lock:
                    hit = self._cache.get(key)
                    if hit is not None:
                        summary, fetched_at, warnings = hit
                        return summary, fetched_at, warnings, True

                    try:
                        raw = await self._adapter.get_schema(self._scope)
                        summary, warnings = build_schema_summary(raw, self._scope, self._database)
                        if asyncio.get_running_loop().time() >= deadline:
                            raise DataSourceTimeout()
                    except Exception:
                        self._status.last_check_status = "error"
                        self._status.last_check_at = self._now_fn()
                        raise

                    fetched_at = self._now_fn()
                    self._cache.set(key, (summary, fetched_at, warnings))
                    self._status.last_check_status = "ok"
                    self._status.last_check_at = fetched_at
                    return summary, fetched_at, warnings, False
        except TimeoutError:
            # 整体截止时间到（排队/连接/查询/清理累计），映射为超时
            self._status.last_check_status = "error"
            self._status.last_check_at = self._now_fn()
            raise DataSourceTimeout() from None

    def invalidate(self) -> None:
        """内部失效方法；清除本数据源缓存，不对外暴露公网刷新端点。"""
        self._cache.invalidate()

    async def close(self) -> None:
        """释放适配器资源（每操作即开即关的适配器为空操作）。"""
        await self._adapter.close()
