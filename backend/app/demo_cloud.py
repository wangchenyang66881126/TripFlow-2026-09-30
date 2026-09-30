"""固定重庆演示的云端入口：同一发布包内的只读快照，可跨实例/重启复用。

快照由原业务接口构建，真实生成仍使用 app.main，不在此开启。
地图继续实时访问百度，预设长图在打包时使用原导出器生成。
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import baidu_map
from .core.admission import AdmissionMiddleware
from .core.config import PROJECT_ROOT, settings
from .core.errors import AppError, error_body
from .core.logging import setup_logging


def create_app(bundle_dir: Path | None = None, frontend_dir: Path | None = None) -> FastAPI:
    if not settings.preset_demo:
        raise RuntimeError("Cloud demo requires PRESET_DEMO=true; real generation needs a separate deployment")
    bundle = bundle_dir or PROJECT_ROOT / "demo-artifacts"
    frontend = frontend_dir or PROJECT_ROOT / "frontend" / "dist"
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("trip_id") != "cqpreset01" or manifest.get("schema_version") != 1:
        raise RuntimeError("Invalid demo artifact manifest")
    # 启动时校验全部发布产物，避免缺图或文件被更改后仍宣称启动成功。
    for name, digest in manifest["sha256"].items():
        file = (bundle / name).resolve()
        if not file.is_relative_to(bundle.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Demo artifact integrity check failed")
    setup_logging()
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                                     limits=httpx.Limits(max_connections=16, max_keepalive_connections=16)) as client:
            app.state.baidu_http_client = client
            yield

    app = FastAPI(title="途书旅记 · 固定演示", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(AdmissionMiddleware,
                       limit=int(os.getenv("TRIPFLOW_MAX_INFLIGHT", "64")),
                       map_limit=int(os.getenv("TRIPFLOW_MAP_INFLIGHT", "16")))
    app.include_router(baidu_map.router, prefix="/api/v1")

    @app.exception_handler(AppError)
    async def known_error(_, exc):
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unknown_error(_, exc):
        return JSONResponse(error_body("INTERNAL", "服务暂时不可用，请稍后重试。"), status_code=500)

    @app.get("/healthz", include_in_schema=False)
    async def health():
        return {"status": "ok", "mode": "preset", "version": manifest["version"]}

    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"])
    async def serve(path: str, request: Request):
        method = "GET" if request.method == "HEAD" else request.method
        key = f"{method} /{path}"
        if key in manifest["responses"]:
            return JSONResponse(manifest["responses"][key], headers={"Cache-Control": "no-store"})
        file_entry = manifest["files"].get(key)
        if key == "GET /api/v1/trips/cqpreset01/map":
            day = request.query_params.get("day", "all")
            file_entry = manifest["maps"].get(day)
            if file_entry is None:
                return JSONResponse(error_body("NO_POINTS", "无坐标可绘制"), status_code=422)
        if file_entry:
            file = (bundle / file_entry["path"]).resolve()
            if not file.is_relative_to(bundle.resolve()) or file_entry["path"] not in manifest["sha256"]:
                return JSONResponse(error_body("INTERNAL", "演示文件不可用"), status_code=500)
            return FileResponse(file, media_type=file_entry["media_type"],
                                filename=file_entry.get("filename"),
                                headers={"Cache-Control": "public, max-age=3600"})
        if path == "api/v1/trips" and method == "POST":
            return JSONResponse(error_body("DEMO_MODE_ONLY", "当前为固定演示，请刷新首页后体验重庆两日游。"), status_code=409)
        if path.startswith("api/v1/trips/cqpreset01/") and method in {"POST", "PATCH", "PUT", "DELETE"}:
            return JSONResponse(error_body("PRESET_READ_ONLY", "这是固定演示行程，暂不支持修改或重新规划。"), status_code=409)
        if method == "GET" and path in {"", "share/cqpreset01"}:
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-store"})
        return JSONResponse(error_body("NOT_FOUND", "资源不存在"), status_code=404)

    return app
