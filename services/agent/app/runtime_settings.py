"""运行时设置存储：管理员经设置 API 持久化的 LLM / 数据源配置。

存储位置：与会话库同目录的 ``runtime_settings.json``（默认 ``data/`` 下），
文件权限 600。环境变量仍是默认值来源；运行时保存的配置覆盖环境变量，
使私有部署的管理员无需改文件、重启即可换模型或换数据源。

安全边界：
- 仅服务端读写；API 响应只回写掩码（api_key_set / password_set），绝不回显密钥；
- 原子写入（同目录临时文件 + os.replace），损坏文件按空配置处理，不崩溃；
- 写入前由路由层完成全部校验（地址、端口、白名单、上限），非法值不落盘；
- 密钥/密码以明文存于该文件（私有部署单机会话库同级），不落日志、不进响应。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import SecretStr

# 可持久化的 LLM 字段（Settings 字段名 → runtime_settings.json 键名一致）。
LLM_SETTING_KEYS = (
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_timeout_seconds",
    "llm_max_tokens",
    "llm_temperature",
)

# 可持久化的数据源字段。
DATASOURCE_SETTING_KEYS = (
    "datasource_enabled",
    "datasource_name",
    "db_host",
    "db_port",
    "db_name",
    "db_user",
    "db_password",
    "db_ssl_enabled",
    "db_allowed_objects_json",
)


class RuntimeSettingsStore:
    """runtime_settings.json 的读写封装；实例不可变路径，方法幂等。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def from_session_db_path(cls, session_db_path: str) -> "RuntimeSettingsStore":
        """会话库所在目录即运行时数据目录；与 SQLite 会话库同级。"""
        return cls(Path(session_db_path).resolve().parent / "runtime_settings.json")

    def load(self) -> dict[str, dict[str, Any]]:
        """读取全部运行时配置；文件缺失/损坏/结构非法一律按空配置处理。"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for section in ("llm", "datasource"):
            section_value = raw.get(section)
            if isinstance(section_value, dict):
                result[section] = section_value
        return result

    def save_llm(self, values: dict[str, Any]) -> None:
        """合并写入 LLM 段；None 值丢弃，调用方决定“缺省=保留原值”。"""
        self._merge_section("llm", values)

    def save_datasource(self, values: dict[str, Any]) -> None:
        """合并写入数据源段；None 值丢弃。"""
        self._merge_section("datasource", values)

    # ---- 内部 ----

    def _merge_section(self, section: str, values: dict[str, Any]) -> None:
        current = self.load()
        merged = dict(current.get(section, {}))
        for key, value in values.items():
            if value is None:
                continue
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            merged[key] = value
        current[section] = merged
        self._write_atomic(current)

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        """先写同目录临时文件再替换，避免中途截断；尽力收紧文件权限。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass  # 权限收紧失败不阻断写入；原子替换仍是主保障。
        os.replace(tmp_path, self._path)


def apply_runtime_overrides(settings: "Settings", overrides: dict[str, dict[str, Any]]) -> "Settings":
    """把运行时配置覆盖到 Settings 上，返回新实例（原实例不被修改）。

    只接受白名单字段；未知键忽略。SecretStr 字段自动包装。
    """
    from app.config import Settings  # 延迟导入避免环

    update: dict[str, Any] = {}
    for section, keys in (
        ("llm", LLM_SETTING_KEYS),
        ("datasource", DATASOURCE_SETTING_KEYS),
    ):
        section_values = overrides.get(section, {})
        for key in keys:
            if key in section_values:
                update[key] = section_values[key]

    for secret_field in ("llm_api_key", "db_password"):
        value = update.get(secret_field)
        if value is not None and not isinstance(value, SecretStr):
            update[secret_field] = SecretStr(str(value))

    if not update:
        return settings
    return settings.model_copy(update=update)  # type: ignore[return-value]


def llm_snapshot(settings: "Settings") -> dict[str, Any]:
    """LLM 配置的脱敏快照；密钥只以 api_key_set 布尔位出现。"""
    return {
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "timeout_seconds": settings.llm_timeout_seconds,
        "max_tokens": settings.llm_max_tokens,
        "temperature": settings.llm_temperature,
        "configured": bool(
            settings.llm_base_url
            and settings.llm_model
            and settings.llm_api_key.get_secret_value()
        ),
        "api_key_set": bool(settings.llm_api_key.get_secret_value()),
    }


def datasource_snapshot(settings: "Settings") -> dict[str, Any]:
    """数据源配置的脱敏快照；密码只以 password_set 布尔位出现。"""
    allowed_objects: dict[str, list[str]] = {}
    try:
        parsed = json.loads(settings.db_allowed_objects_json or "{}")
        if isinstance(parsed, dict):
            for obj, cols in parsed.items():
                if isinstance(obj, str) and isinstance(cols, list):
                    allowed_objects[obj] = [c for c in cols if isinstance(c, str)]
    except ValueError:
        allowed_objects = {}
    return {
        "enabled": settings.datasource_enabled,
        "id": settings.datasource_id,
        "name": settings.datasource_name,
        "host": settings.db_host,
        "port": settings.db_port,
        "database": settings.db_name,
        "user": settings.db_user,
        "ssl_enabled": settings.db_ssl_enabled,
        "password_set": bool(settings.db_password.get_secret_value()),
        "allowed_objects": allowed_objects,
    }
