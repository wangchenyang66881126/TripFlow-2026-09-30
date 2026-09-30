"""只在本机启动两个独立实例，验证读取一致、重启及并发；不向百度发压测请求。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(package, port):
    env = {**os.environ, "PRESET_DEMO": "true", "DEEPSEEK_API_KEY": "",
           "BAIDU_MAP_AK": "", "BAIDU_JSAPI_AK": "", "PYTHONPATH": ""}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                "--port", str(port), "--no-access-log"], cwd=package, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process


async def wait_ready(client, origin):
    for _ in range(100):
        try:
            if (await client.get(origin + "/healthz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(.1)
    raise RuntimeError("Local demo failed to start")


async def check(package):
    ports = [available_port(), available_port()]
    origins = [f"http://127.0.0.1:{port}" for port in ports]
    processes = [start(package, port) for port in ports]
    manifest = json.loads((package / "demo-artifacts/manifest.json").read_text())
    results = {"scope": "local HTTP only; real frozen data; no live Baidu load; NOT a cloud capacity guarantee",
               "package_version": manifest["version"], "stages": []}
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False, limits=httpx.Limits(max_connections=600,
                                     max_keepalive_connections=100)) as client:
            await asyncio.gather(*[wait_ready(client, origin) for origin in origins])
            for key, expected in manifest["responses"].items():
                method, path = key.split(" ", 1)
                for origin in origins:
                    response = await client.request(method, origin + path)
                    assert response.status_code == 200 and response.json() == expected, key
            results["cross_instance_response_parity"] = True
            image_path = "/api/v1/trips/cqpreset01/export.png"
            image_before = (await client.get(origins[0] + image_path)).content
            assert image_before.startswith(b"\x89PNG")
            assert hashlib.sha256(image_before).hexdigest() == manifest["sha256"]["export.png"]
            processes[0].terminate()
            await asyncio.to_thread(processes[0].wait, 10)
            processes[0] = start(package, ports[0])
            await wait_ready(client, origins[0])
            assert (await client.get(origins[0] + image_path)).content == image_before
            assert (await client.get(origins[0] + "/api/v1/tasks/preset-export-v1")).json()["status"] == "done"
            results["restart_export_restored"] = True
            root = "/api/v1/trips/cqpreset01"

            for count in [20, 100, 500]:
                statuses, errors, latencies = Counter(), [], []
                successful_journeys = 0

                async def request(method, path, user):
                    start_time = time.perf_counter()
                    try:
                        # 每个请求交替落到两个独立进程，验证没有会话黏性要求。
                        origin = origins[(user + len(latencies)) % 2]
                        response = await client.request(method, origin + path)
                        statuses[str(response.status_code)] += 1
                        if response.status_code == 503:
                            assert response.json()["error"]["code"] == "SERVICE_BUSY"
                        return response.status_code == 200
                    except (httpx.HTTPError, AssertionError) as exc:
                        errors.append(type(exc).__name__)
                        return False
                    finally:
                        latencies.append(time.perf_counter() - start_time)

                async def journey(user):
                    nonlocal successful_journeys
                    opened = await request("POST", "/api/v1/trips/preset", user)
                    # 与前端同样读取行程/路线/酒店/照片/导航；另验证导出与分享。
                    reads = await asyncio.gather(*[request("GET", root + "/" + path, user)
                                                  for path in ["places", "route", "hotels", "photos", "app-nav"]])
                    exported = await request("POST", root + "/export", user)
                    done = await request("GET", "/api/v1/tasks/preset-export-v1", user)
                    downloaded = await request("GET", image_path, user)
                    share = await request("GET", "/api/v1/share/cqpreset01", user)
                    successful_journeys += int(all([opened, *reads, exported, done, downloaded, share]))

                began = time.perf_counter()
                await asyncio.gather(*[journey(i) for i in range(count)])
                elapsed = time.perf_counter() - began
                latencies.sort()
                result = {"simulated_users": count, "requests": sum(statuses.values()), "status_counts": dict(statuses),
                          "successful_journeys": successful_journeys, "network_or_protocol_errors": dict(Counter(errors)),
                          "elapsed_s": round(elapsed, 3), "p95_request_ms": round(latencies[int(.95 * (len(latencies)-1))] * 1000, 1)}
                results["stages"].append(result)
                print(json.dumps(result), flush=True)
            assert (await client.post(origins[0] + "/api/v1/trips/preset")).status_code == 200
            results["recovered_after_burst"] = True
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(check(args.package.resolve()))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
