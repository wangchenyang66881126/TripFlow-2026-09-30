import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.demo_cloud import create_app


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "preset_demo", True)
    root = tmp_path / "bundle"
    root.mkdir()
    front = tmp_path / "frontend"
    (front / "assets").mkdir(parents=True)
    (front / "index.html").write_text("<h1>途书旅记</h1>")
    # 测试载荷不冒充真实地图；真实发布包另用原业务导出器构建验证。
    (root / "export.png").write_bytes(b"fixture-export")
    manifest = {"trip_id": "cqpreset01", "schema_version": 1, "version": "test",
                "responses": {"POST /api/v1/trips/preset": {"trip_id": "cqpreset01", "status": "done", "preset": True},
                              "POST /api/v1/trips/cqpreset01/export": {"task_id": "preset-export-v1"},
                              "GET /api/v1/tasks/preset-export-v1": {"status": "done"}},
                "files": {"GET /api/v1/trips/cqpreset01/export.png": {"path": "export.png", "media_type": "image/png"}},
                "maps": {}, "sha256": {"export.png": hashlib.sha256(b"fixture-export").hexdigest()}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, front


def test_export_task_and_share_survive_new_instances_without_database(bundle):
    with TestClient(create_app(*bundle)) as first:
        created = first.post("/api/v1/trips/cqpreset01/export").json()
    with TestClient(create_app(*bundle)) as restarted:
        assert restarted.get("/api/v1/tasks/" + created["task_id"]).json()["status"] == "done"
        assert restarted.get("/api/v1/trips/cqpreset01/export.png").content == b"fixture-export"
        assert restarted.get("/share/cqpreset01").status_code == 200
        assert restarted.get("/healthz").json()["mode"] == "preset"


def test_private_history_and_real_generation_are_not_exposed(bundle):
    with TestClient(create_app(*bundle)) as client:
        for path in ["/api/v1/trips/oldprivate/places", "/api/v1/tasks/private", "/api/v1/share/private", "/share/private", "/.env", "/docs", "/openapi.json", "/api/v1/ai-budget"]:
            assert client.get(path).status_code == 404
        assert client.post("/api/v1/trips", json={"source_link": "新攻略"}).status_code == 409
        assert client.patch("/api/v1/trips/cqpreset01/places/1", json={"name": "改变"}).status_code == 409
        assert client.post("/api/v1/trips/cqpreset01/route").status_code == 409
        assert client.get("/api/v1/trips/cqpreset01/map?day=999").status_code == 422


def test_artifact_tampering_prevents_startup(bundle):
    (bundle[0] / "export.png").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="integrity"):
        create_app(*bundle)


def test_cloud_entry_refuses_real_generation_mode(bundle, monkeypatch):
    monkeypatch.setattr(settings, "preset_demo", False)
    with pytest.raises(RuntimeError, match="PRESET_DEMO"):
        create_app(*bundle)
