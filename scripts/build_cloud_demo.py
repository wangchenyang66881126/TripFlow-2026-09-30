"""从原业务接口构建公开演示包，不带历史数据库、密钥或真实生成入口。

由 backend/.venv/bin/python 运行；在隔离子进程使用临时数据库。
构建会请求三张真实百度静态地图并用原导出器生成固定长图。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def snapshot(output: Path) -> None:
    sys.path.insert(0, str(ROOT / "backend"))
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.config import ASSETS_DIR
    from app.core.db import SessionLocal
    from app.models import Trip
    from app.services import export, map as maps, pipeline, preset

    target = output / "demo-artifacts"
    target.mkdir(parents=True)
    manifest = {"schema_version": 1, "trip_id": preset.TRIP_ID,
                "responses": {}, "files": {}, "maps": {}, "sha256": {}}

    def write_file(name: str, data: bytes, media: str) -> dict:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest["sha256"][name] = hashlib.sha256(data).hexdigest()
        return {"path": name, "media_type": media}

    with TestClient(app) as client:
        prefix = f"/api/v1/trips/{preset.TRIP_ID}"
        paths = [prefix, *[prefix + '/' + p for p in ["places", "route", "hotels", "photos", "app-nav"]],
                 f"/api/v1/share/{preset.TRIP_ID}"]
        opened = client.post("/api/v1/trips/preset")
        opened.raise_for_status()
        manifest["responses"]["POST /api/v1/trips/preset"] = opened.json()
        for path in paths:
            response = client.get(path)
            response.raise_for_status()
            manifest["responses"][f"GET {path}"] = response.json()
        assert len(manifest["responses"][f"GET {prefix}/places"]["places"]) == 16
        for place_id, photo in manifest["responses"][f"GET {prefix}/photos"]["photos"].items():
            response = client.get(photo["src"])
            response.raise_for_status()
            manifest["files"]["GET " + photo["src"]] = write_file(f"photos/{place_id}.jpg", response.content, "image/jpeg")

    # 三张真实底图，导出时复用日地图；不会生成付费 AI 行程。
    cached_maps = {}
    fetch_map = maps.build_static_map_png

    def cached_map(points, width=640, height=400):
        key = json.dumps([points, width, height], sort_keys=True)
        if key not in cached_maps:
            cached_maps[key] = fetch_map(points, width, height)
        return cached_maps[key]

    maps.build_static_map_png = cached_map
    with SessionLocal() as db:
        trip = db.get(Trip, preset.TRIP_ID)
        places = sorted(trip.places, key=lambda p: (p.day, p.seq))
        for day in ("all", "1", "2"):
            selected = [p for p in places if day == "all" or p.day == int(day)]
            png = cached_map([{"lat": p.lat, "lng": p.lng} for p in selected])
            manifest["maps"][day] = write_file(f"maps/{day}.png", png, "image/png")
        route = pipeline.load_route_json(preset.TRIP_ID)
        image = Path(export.export_long_image(trip, route["days"], {p.id: p for p in places}))
        entry = write_file("export.png", image.read_bytes(), "image/png")
        entry["filename"] = f"trip-{preset.TRIP_ID}.png"
        manifest["files"][f"GET {prefix}/export.png"] = entry

    task_id = "preset-export-v1"
    manifest["responses"][f"POST {prefix}/export"] = {"trip_id": preset.TRIP_ID, "task_id": task_id}
    manifest["responses"][f"GET /api/v1/tasks/{task_id}"] = {
        "id": task_id, "trip_id": preset.TRIP_ID, "kind": "export", "status": "done",
        "progress": "长图已生成", "error": None, "result_path": None,
    }
    manifest["version"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def build(output: Path) -> None:
    if output.exists():
        raise RuntimeError("Output already exists; use a fresh output directory")
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend", check=True,
                   env={**os.environ, "VITE_PRESET_DEMO": "true"})
    with tempfile.TemporaryDirectory(prefix="tripflow-build-") as temp:
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--snapshot", "--output", str(output)],
                       env={**os.environ, "TRIPFLOW_DATA_DIR": temp,
                            "DATABASE_URL": f"sqlite:///{temp}/demo.db", "PRESET_DEMO": "true",
                            "DEEPSEEK_API_KEY": ""}, check=True)
    # 明确白名单，整个仓库、.env、数据库、历史用户文件均不会进入产物。
    for name in ["__init__.py", "demo_cloud.py", "api/__init__.py", "api/baidu_map.py",
                 "core/__init__.py", "core/admission.py", "core/config.py", "core/errors.py", "core/logging.py"]:
        dest = output / "backend" / "app" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "backend" / "app" / name, dest)
    shutil.copytree(ROOT / "frontend" / "dist", output / "frontend" / "dist")
    (output / "main.py").write_text('''import sys
from pathlib import Path
if sys.platform == "linux":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "site-packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
from app.demo_cloud import create_app
app = create_app()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1, access_log=False)
''')
    (output / "requirements.txt").write_text("\n".join(
        f"{name}=={importlib.metadata.version(name)}" for name in ["fastapi", "uvicorn", "httpx", "python-dotenv"]) + "\n")
    # 运行目标为 Native Python 3.12 Linux x86_64，不能打包 Mac 的二进制依赖。
    subprocess.run([sys.executable, "-m", "pip", "install", "--platform", "manylinux2014_x86_64",
                    "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312",
                    "--only-binary=:all:", "--target", str(output / "site-packages"),
                    "-r", str(output / "requirements.txt"), "--no-compile", "--disable-pip-version-check"], check=True)
    locked = sorted(f"{d.metadata['Name']}=={d.version}" for d in
                    importlib.metadata.distributions(path=[str(output / "site-packages")]))
    (output / "requirements.txt").write_text("\n".join(locked) + "\n")
    # 独立检查构建文件是否带入本机密钥，只报告是否通过，不输出值。
    from dotenv import dotenv_values
    secrets = []
    for file in [ROOT / ".env", ROOT / "backend/.env", ROOT / "frontend/.env.local"]:
        for key, value in dotenv_values(file).items():
            if value and len(value) >= 8 and any(part in key.upper() for part in ["KEY", "AK", "SECRET", "TOKEN", "PASSWORD"]):
                secrets.append(value.encode())
    for file in output.rglob("*"):
        if not file.is_file():
            continue
        if file.name.startswith(".env") or file.suffix in {".db", ".sqlite", ".sqlite3"}:
            raise RuntimeError("Forbidden file in deployment package")
        content = file.read_bytes()
        if any(secret in content for secret in secrets):
            raise RuntimeError("Secret scan failed; do not upload this package")
    print(json.dumps({"package": str(output), "secret_scan": "passed",
                      "bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file())}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        (snapshot if args.snapshot else build)(args.output.resolve())
    except Exception as exc:
        # 外部 API 异常可能含请求 URL，构建失败不回显堆栈或凭据。
        print(f"Build failed ({type(exc).__name__}); package is NOT approved for upload.", file=sys.stderr)
        sys.exit(1)
