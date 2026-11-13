"""Explicit offline simulation, never silently used in production mode."""

import time
from typing import Any

import httpx


class DemoTransport:
    def __init__(self) -> None:
        self.channel_status = 1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/channel/":
            data: Any = [
                {
                    "id": 1,
                    "name": "DEMO 主渠道",
                    "status": self.channel_status,
                    "balance": 168.0,
                    "response_time": 320,
                    "models": "demo-model",
                },
                {
                    "id": 2,
                    "name": "DEMO 备用渠道",
                    "status": 2,
                    "balance": 42.0,
                    "response_time": 280,
                },
            ]
        elif path == "/api/log/":
            end = int(request.url.params.get("end_timestamp", int(time.time())))
            start = int(request.url.params.get("start_timestamp", end - 86400))
            # IDs and data are stable across retries and overlapping windows.
            data = [
                {
                    "id": bucket,
                    "created_at": bucket * 3600,
                    "type": 2,
                    "channel": 1,
                    "model_name": "demo-model",
                    "user_id": 1,
                    "username": "demo",
                    "quota": (bucket % 5 + 1) * 1000,
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "use_time": 1,
                }
                for bucket in range(start // 3600, end // 3600 + 1)
                if start <= bucket * 3600 <= end
            ]
        elif path == "/api/user/search":
            data = [
                {
                    "id": 1,
                    "username": "demo",
                    "display_name": "演示用户",
                    "quota": 100000,
                    "used_quota": 12000,
                    "request_count": 100,
                    "group": "demo",
                }
            ]
        elif path.startswith("/api/user/") and path.rsplit("/", 1)[-1].isdigit():
            user_id = int(path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": user_id,
                        "username": f"demo-{user_id}",
                        "quota": 100000,
                        "used_quota": 12000,
                        "request_count": 100,
                    },
                },
            )
        elif path.startswith("/api/channel/test/"):
            return httpx.Response(200, json={"success": True, "time": 0.32})
        else:
            return httpx.Response(404, json={"success": False})
        page = int(request.url.params.get("p", "1"))
        size = int(request.url.params.get("page_size", "100"))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"items": data[(page - 1) * size : page * size], "total": len(data)},
            },
        )
