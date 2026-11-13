import sys

import pytest

from newapi_watch.__main__ import main


@pytest.mark.parametrize("action", ["check", "once"])
def test_cli_demo_config_and_one_shot(tmp_path, monkeypatch, capsys, action):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["newapi-watch", "--demo", action])
    assert main() == 0
    output = capsys.readouterr().out
    assert "DEMO" in output or "demo=True" in output


def test_cli_demo_query_and_png(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "report.png"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "newapi-watch",
            "--demo",
            "--database",
            str(tmp_path / "demo.db"),
            "command",
            "图表 今天",
            "--image",
            str(image),
        ],
    )
    assert main() == 0
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "DEMO" in capsys.readouterr().out


def test_cli_production_config_failure_does_not_echo_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WATCH_NEWAPI_TOKEN", "highly-sensitive")
    monkeypatch.setattr(sys, "argv", ["newapi-watch", "check"])
    assert main() == 2
    assert "highly-sensitive" not in capsys.readouterr().err


def test_cli_pipe_is_utf8_even_with_windows_legacy_locale(tmp_path):
    import os
    import subprocess

    env = os.environ.copy()
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONUTF8"] = "0"
    result = subprocess.run(
        [sys.executable, "-m", "newapi_watch", "--demo", "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert "配置有效" in result.stdout.decode("utf-8")
