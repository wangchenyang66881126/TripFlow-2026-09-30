"""单进程 ASGI 并发准入；只保存临时计数，不承载业务或任务状态。"""
from __future__ import annotations

from starlette.responses import JSONResponse

from .errors import error_body


class AdmissionMiddleware:
    def __init__(self, app, limit: int = 64, map_limit: int = 16):
        if not 0 < map_limit <= limit:
            raise ValueError("Expected 0 < map_limit <= limit")
        self.app = app
        self.limit = limit
        self.map_limit = map_limit
        self.active = 0
        self.maps = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        is_map = scope["path"].startswith("/api/v1/baidu-map/")
        # 检查与递增之间没有 await，同一事件循环内不会超额发放名额。
        if self.active >= self.limit or (is_map and self.maps >= self.map_limit):
            response = JSONResponse(
                error_body("SERVICE_BUSY", "当前访问较多，请稍后重试。"),
                status_code=503,
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        self.active += 1
        self.maps += int(is_map)
        try:
            await self.app(scope, receive, send)
        finally:
            # 响应传输结束、异常、断开或取消均释放；不创建后台排队任务。
            self.active -= 1
            self.maps -= int(is_map)
