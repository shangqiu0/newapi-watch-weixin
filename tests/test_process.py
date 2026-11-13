"""A real TCP/process acceptance test; it never contacts NewAPI or WeCom."""

import os
import queue
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx


def test_demo_real_http_process(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        WATCH_API_KEY="local-test-key-not-a-real-credential",
        WATCH_REPORT_ENABLED="false",
        PYTHONIOENCODING="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "newapi_watch",
            "--demo",
            "--database",
            str(tmp_path / "process.db"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    lines = queue.Queue()

    def read_output():
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    captured = []
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            captured.append(line)
            if "Uvicorn running on" in line:
                break
        else:
            raise AssertionError("Server did not start: " + "".join(captured))
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False
        ) as client:
            response = client.get("/health/live")
            assert response.status_code == 200, (
                f"local health HTTP {response.status_code}, content_type={response.headers.get('content-type')}, body_length={len(response.content)}"
            )
            assert response.json()["data"]["demo"] is True
            assert client.get("/health/ready").status_code == 200
            assert client.get("/api/v1/channels").status_code == 401
            client.headers["X-API-Key"] = env["WATCH_API_KEY"]
            response = client.get("/api/v1/channels")
            assert response.json()["data"]["total"] == 2
            assert client.post("/api/v1/commands", json={"text": "用户 demo"}).status_code == 200
            chart = client.post("/api/v1/commands", json={"text": "图表 今天"}).json()
            assert chart["data"]["has_image"] is True
            assert "DEMO" in chart["data"]["text"]
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            png = client.get(f"/api/v1/reports/{day}/chart")
            assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
            artifacts = root / "artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "demo-report.png").write_bytes(png.content)
            (artifacts / "http-smoke.json").write_text(response.text, encoding="utf-8")
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            process.terminate()
        process.wait(timeout=10)
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
