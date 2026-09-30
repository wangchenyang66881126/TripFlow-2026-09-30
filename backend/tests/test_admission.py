"""验证突发请求、客户端取消及上游异常后不会耗尽所有服务名额。"""
import asyncio

import httpx
from starlette.responses import JSONResponse

from app.core.admission import AdmissionMiddleware


def test_500_simultaneous_requests_fail_fast_then_recover():
    async def run():
        release = asyncio.Event()
        async def slow_app(scope, receive, send):
            await release.wait()
            await JSONResponse({"ok": True})(scope, receive, send)
        app = AdmissionMiddleware(slow_app, limit=32, map_limit=16)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            jobs = [asyncio.create_task(client.get("/api/v1/trips/preset")) for _ in range(500)]
            for _ in range(1000):
                if sum(job.done() for job in jobs) == 468:
                    break
                await asyncio.sleep(.001)
            assert app.active == 32
            assert sum(job.done() for job in jobs) == 468
            busy = [job.result() for job in jobs if job.done()]
            assert all(r.status_code == 503 and r.headers["retry-after"] == "3" for r in busy)
            assert all(r.json()["error"]["code"] == "SERVICE_BUSY" for r in busy)
            release.set()
            responses = await asyncio.gather(*jobs)
            assert sum(r.status_code == 200 for r in responses) == 32
            assert app.active == app.maps == 0
            assert (await client.get("/api/v1/trips/preset")).status_code == 200
    asyncio.run(run())


def test_map_requests_leave_capacity_for_trip_data():
    async def run():
        release = asyncio.Event()
        async def app(scope, receive, send):
            if "baidu-map" in scope["path"]:
                await release.wait()
            await JSONResponse({"ok": True})(scope, receive, send)
        gate = AdmissionMiddleware(app, limit=4, map_limit=2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://test") as client:
            jobs = [asyncio.create_task(client.get("/api/v1/baidu-map/api")) for _ in range(2)]
            while gate.maps < 2:
                await asyncio.sleep(0)
            assert (await client.get("/api/v1/baidu-map/api")).status_code == 503
            assert (await client.get("/api/v1/trips/preset")).status_code == 200
            assert (await client.get("/healthz")).status_code == 200
            jobs[0].cancel()
            try:
                await jobs[0]
            except asyncio.CancelledError:
                pass
            assert gate.maps == 1
            release.set()
            await jobs[1]
            assert gate.active == gate.maps == 0
    asyncio.run(run())


def test_exception_releases_capacity():
    async def run():
        async def broken(scope, receive, send):
            raise RuntimeError("test failure")
        gate = AdmissionMiddleware(broken, limit=1, map_limit=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://test") as client:
            try:
                await client.get("/api/v1/test")
            except RuntimeError:
                pass
        assert gate.active == gate.maps == 0
    asyncio.run(run())
