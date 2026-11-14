"""Render real Compose configuration with synthetic env files, never real credentials."""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose_config(tmp_path: Path) -> Callable[..., dict[str, Any]]:
    docker = shutil.which("docker")
    if docker is None or not (ROOT / "compose.yaml").exists():
        pytest.skip("Requires Docker Compose CLI and repository deployment files")
    available = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=15
    )
    if available.returncode != 0:
        pytest.skip("Docker Compose plugin is not installed")
    for name in ("compose.yaml", "compose.console.yaml"):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, tmp_path / name)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WATCH_", "COMPOSE_"))
    }

    def render(*, custom_url: str | None = None, console: bool = False) -> dict[str, Any]:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "WATCH_DEMO=true\n"
            + (f"WATCH_QUERY_REDIS_URL={custom_url}\n" if custom_url is not None else ""),
            encoding="utf-8",
        )
        command = [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-p",
            "watch-config-test",
            "-f",
            str(tmp_path / "compose.yaml"),
        ]
        if console:
            command.extend(["-f", str(tmp_path / "compose.console.yaml")])
        result = subprocess.run(
            [*command, "config", "--format", "json"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        data: dict[str, Any] = json.loads(result.stdout)
        return data

    return render


def test_default_compose_connects_app_to_healthy_redis(compose_config: Callable[..., Any]) -> None:
    config = compose_config()
    watch = config["services"]["watch"]
    assert "redis" in config["services"]
    assert watch["environment"]["WATCH_QUERY_REDIS_URL"] == "redis://redis:6379/0"
    assert watch["depends_on"]["redis"]["condition"] == "service_healthy"
    assert config["services"]["redis"]["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]


def test_redis_is_bounded_cache_not_public_database(compose_config: Callable[..., Any]) -> None:
    config = compose_config()
    redis = config["services"].get("redis")
    assert redis is not None, "Compose must start the Redis image"
    assert redis["image"].startswith("redis:") and "latest" not in redis["image"]
    assert not redis.get("ports")
    assert redis["read_only"] and redis["user"] == "redis"
    assert redis["restart"] == "unless-stopped" and int(redis["mem_limit"]) <= 512 * 1024**2
    shared = set(redis["networks"]) & set(config["services"]["watch"]["networks"])
    assert shared and all(config["networks"][name]["internal"] for name in shared)
    assert all(config["networks"][name]["internal"] for name in redis["networks"])
    args = redis["command"]
    assert args[args.index("--appendonly") + 1] == "no"
    assert args[args.index("--save") + 1] == ""
    assert args[args.index("--maxmemory-policy") + 1] == "allkeys-lru"


@pytest.mark.parametrize("url", ["redis://external.invalid:6380/2", ""])
def test_explicit_redis_override_and_memory_only_mode(
    compose_config: Callable[..., Any], url: str
) -> None:
    watch = compose_config(custom_url=url)["services"]["watch"]
    assert watch["environment"]["WATCH_QUERY_REDIS_URL"] == url


def test_console_overlay_keeps_data_volume_and_redis_wiring(
    compose_config: Callable[..., Any],
) -> None:
    config = compose_config(console=True)
    watch = config["services"]["watch"]
    assert watch["command"] == [
        "console",
        "--control-dir",
        "/app/data/console",
        "--host",
        "0.0.0.0",
        "--port",
        "8088",
    ]
    assert watch["environment"]["WATCH_QUERY_REDIS_URL"] == "redis://redis:6379/0"
    assert any(
        volume["target"] == "/app/data" and volume["type"] == "volume"
        for volume in watch["volumes"]
    )
    assert "redis" in watch["depends_on"]
