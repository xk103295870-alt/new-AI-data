"""设置 API 回归测试：LLM/数据源保存、脱敏、探测分类与运行时重建。

网络与真实数据库全部以替身隔离：LLM 探测 monkeypatch，
数据库经 create_app(adapter_factory=...) 注入 Fake Adapter。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routes.settings as settings_routes
from app.main import create_app
from app.config import Settings


class FakeAdapter:
    """只实现设置 API 用到的三个方法；行为由类属性控制。"""

    test_ok = True
    discover_result: tuple[list[dict[str, Any]], bool] = ([], False)

    def __init__(self, config: Any) -> None:
        self.config = config

    async def test_connection(self) -> None:
        if not self.test_ok:
            from app.errors import DataSourceAuthError

            raise DataSourceAuthError()

    async def discover_objects(self) -> tuple[list[dict[str, Any]], bool]:
        return self.discover_result

    async def close(self) -> None:
        return None


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    FakeAdapter.test_ok = True
    FakeAdapter.discover_result = ([], False)
    monkeypatch.setattr(
        settings_routes, "probe_llm_endpoint",
        lambda **kwargs: _async_result((True, 12, None)),
    )
    settings = Settings(session_db_path=str(tmp_path / "sessions" / "agent.sqlite3"))
    app = create_app(settings, adapter_factory=FakeAdapter)
    return TestClient(app)


def _async_result(value: Any):
    import asyncio

    future: Any = asyncio.get_event_loop_policy().new_event_loop().create_future()
    future.set_result(value)

    async def _coro() -> Any:
        return value

    return _coro()


def _llm_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "base_url": "https://llm.example.com/v1",
        "model": "demo-model",
        "timeout_seconds": 60,
        "max_tokens": 2048,
        "temperature": 0.0,
        "api_key": "sk-secret",
    }
    body.update(overrides)
    return body


def _ds_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "enabled": True,
        "name": "测试库",
        "host": "127.0.0.1",
        "port": 3306,
        "database": "demo",
        "user": "ro",
        "ssl_enabled": False,
        "allowed_objects": {"orders": ["id"]},
        "password": "pw",
    }
    body.update(overrides)
    return body


class TestSettingsSnapshot:
    def test_default_unconfigured_and_masked(self, client: TestClient) -> None:
        snap = client.get("/api/v1/settings").json()
        assert snap["data"]["llm"]["configured"] is False
        assert snap["data"]["llm"]["api_key_set"] is False
        assert snap["data"]["datasource"]["enabled"] is False
        assert snap["data"]["datasource"]["password_set"] is False


class TestLlmSettings:
    def test_put_llm_persists_and_masks(self, client: TestClient, tmp_path: Path) -> None:
        resp = client.put("/api/v1/settings/llm", json=_llm_body(temperature=0.5))
        assert resp.status_code == 200
        llm = resp.json()["data"]["llm"]
        assert llm["configured"] and llm["api_key_set"] and llm["temperature"] == 0.5
        assert "sk-secret" not in json.dumps(resp.json())

        stored = json.loads((tmp_path / "sessions" / "runtime_settings.json").read_text())
        assert stored["llm"]["llm_api_key"] == "sk-secret"

    @pytest.mark.parametrize("field,value", [
        ("base_url", "not-a-url"),
        ("timeout_seconds", 999),
        ("max_tokens", 0),
        ("temperature", 2.5),
    ])
    def test_put_llm_invalid_rejected(self, client: TestClient, field: str, value: Any) -> None:
        resp = client.put("/api/v1/settings/llm", json=_llm_body(**{field: value}))
        assert resp.status_code == 400
        assert "sk-secret" not in resp.text

    def test_put_llm_missing_key_rejected(self, client: TestClient) -> None:
        body = _llm_body()
        body["api_key"] = ""
        resp = client.put("/api/v1/settings/llm", json=body)
        assert resp.status_code == 400

    def test_llm_test_uses_probe(self, client: TestClient) -> None:
        client.put("/api/v1/settings/llm", json=_llm_body())
        resp = client.post("/api/v1/settings/llm/test", json={})
        data = resp.json()["data"]
        assert data["ok"] is True and data["latency_ms"] == 12 and data["error"] is None

    def test_llm_test_invalid_config_category(self, client: TestClient) -> None:
        resp = client.post("/api/v1/settings/llm/test", json={"temperature": 9.0})
        data = resp.json()["data"]
        assert data["ok"] is False and data["error"] == "config-invalid"


class TestDatasourceSettings:
    def test_put_datasource_rebuilds_registry(self, client: TestClient) -> None:
        resp = client.put("/api/v1/settings/datasource", json=_ds_body())
        assert resp.status_code == 200
        ds = resp.json()["data"]["datasource"]
        assert ds["enabled"] and ds["password_set"]
        assert ds["allowed_objects"] == {"orders": ["id"]}
        assert "pw" not in json.dumps(resp.json())

        listed = client.get("/api/v1/datasources").json()["data"]["datasources"]
        assert listed[0]["configured"] is True

    def test_put_datasource_wildcard_scope_rejected(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/settings/datasource",
            json=_ds_body(allowed_objects={"bad*name": ["id"]}),
        )
        assert resp.status_code == 400

    def test_datasource_test_success_and_failure(self, client: TestClient) -> None:
        client.put("/api/v1/settings/datasource", json=_ds_body())
        ok = client.post("/api/v1/settings/datasource/test", json={}).json()["data"]
        assert ok["ok"] is True and ok["error"] is None

        FakeAdapter.test_ok = False
        bad = client.post("/api/v1/settings/datasource/test", json={}).json()["data"]
        assert bad["ok"] is False and bad["error"] == "auth"

    def test_datasource_test_invalid_config(self, client: TestClient) -> None:
        result = client.post(
            "/api/v1/settings/datasource/test", json={"port": 70000}
        ).json()["data"]
        assert result["ok"] is False and result["error"] == "config-invalid"

    def test_discover_tables(self, client: TestClient) -> None:
        client.put("/api/v1/settings/datasource", json=_ds_body())
        FakeAdapter.discover_result = (
            [{"name": "orders", "kind": "table", "comment": "", "columns": [
                {"name": "id", "data_type": "bigint", "nullable": False, "comment": ""},
            ]}],
            False,
        )
        data = client.get("/api/v1/settings/datasource/tables").json()["data"]
        assert data["objects"][0]["name"] == "orders"
        assert data["objects"][0]["columns"][0]["name"] == "id"
        assert data["truncated"] is False

    def test_discover_tables_requires_config(self, client: TestClient) -> None:
        resp = client.get("/api/v1/settings/datasource/tables")
        assert resp.status_code == 400
