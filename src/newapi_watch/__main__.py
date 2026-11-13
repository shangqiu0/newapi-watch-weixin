"""CLI entry point for production serving, offline demo and local commands."""

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from .api import create_app
from .config import Settings
from .runtime import Runtime


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="NewAPI 独立监控 · 所有使用者权限一致")
    result.add_argument(
        "--demo", action="store_true", help="显式使用离线模拟数据，不向企微发送消息"
    )
    result.add_argument("--database", type=Path, help="覆盖 SQLite 路径")
    sub = result.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve", help="启动采集、告警、企微与 HTTP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8088)
    command = sub.add_parser("command", help="执行一条查询/报表命令")
    command.add_argument("text")
    command.add_argument("--image", type=Path, help="将图表保存为 PNG")
    sub.add_parser("check", help="校验配置，不连接外部服务")
    sub.add_parser("once", help="采集一次渠道与日志，不发送通知")
    for name, description in (
        ("console", "启动多平台Web管理台"),
        ("console-init", "创建唯一管理账号和随机初始密码文件"),
        ("console-import-env", "显式导入原.env为停用平台，不连接机器人"),
    ):
        item = sub.add_parser(name, help=description)
        item.add_argument("--control-dir", type=Path, default=Path("data/console"))
        if name == "console-init":
            item.add_argument("--username", default="admin")
        if name == "console":
            item.add_argument("--host", default="127.0.0.1")
            item.add_argument("--port", type=int, default=8090)
            item.add_argument(
                "--insecure-cookie", action="store_true", help="仅限本机HTTP开发；公网必须HTTPS"
            )

    return result


async def local(settings: Settings, args: argparse.Namespace) -> int:
    runtime = Runtime(settings)
    try:
        await runtime.open(start_tasks=False)
        channels_ok = await runtime.monitor.collect_channels()
        logs_ok = await runtime.monitor.collect_logs()
        if args.action == "once":
            print(f"channels_ok={channels_ok} logs_ok={logs_ok} demo={settings.demo}")
            return 0 if channels_ok and logs_ok else 1
        response = await runtime.commands.execute(args.text)
        print(response.text)
        if args.image and response.image:
            args.image.parent.mkdir(parents=True, exist_ok=True)
            args.image.write_bytes(response.image)
            print(f"PNG: {args.image.resolve()}")
        return 0
    finally:
        await runtime.close()


def main() -> int:
    # Windows pipes otherwise use the legacy locale while PowerShell expects UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not stream.isatty() and callable(reconfigure):
            reconfigure(encoding="utf-8")
    args = parser().parse_args()
    if args.action.startswith("console"):
        from .console import import_legacy, initialize, serve_console
        from .control_store import ControlError

        try:
            if args.action == "console-init":
                destination = asyncio.run(initialize(args.control_dir, args.username))
                print(f"管理账号已初始化。初始凭证仅写入本机文件：{destination.resolve()}")
            elif args.action == "console-import-env":
                asyncio.run(import_legacy(args.control_dir))
                print("配置已导入为停用平台，未连接机器人、未发送通知。")
            else:
                serve_console(
                    args.control_dir,
                    host=args.host,
                    port=args.port,
                    demo=args.demo,
                    secure_cookie=not args.insecure_cookie,
                )
            return 0
        except (ControlError, ValueError, RuntimeError, OSError) as exc:
            print(
                f"管理台操作未完成（{type(exc).__name__}），请检查是否已初始化及目录权限。",
                file=sys.stderr,
            )
            return 2
    values: dict[str, object] = {}
    if args.demo:
        values.update(demo=True, database=Path("data/demo.sqlite3"))
    if args.database:
        values["database"] = args.database
    try:
        settings = Settings(**values)  # type: ignore[arg-type]
    except ValidationError:
        print(
            "配置不合法：请核对 .env.example 中的凭证、地址和范围；未打印敏感值。", file=sys.stderr
        )
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.action == "check":
        print(f"配置有效：mode={'DEMO' if settings.demo else 'production'}；permissions=shared")
        return 0
    if args.action != "serve":
        return asyncio.run(local(settings, args))

    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await runtime.open()
            yield
        finally:
            await runtime.close()

    app = create_app(settings, runtime.store, runtime.commands, runtime.monitor, lifespan=lifespan)
    app.state.runtime = runtime
    # Multiple workers would duplicate collectors and the single WeCom connection.
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
