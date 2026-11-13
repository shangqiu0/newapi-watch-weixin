"""Console provisioning and an explicit, non-invasive legacy configuration import."""

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from .auth import Auth
from .console_api import create_console
from .control_models import BindingInput, LLMConfig, PlatformInput
from .control_store import ControlError, ControlStore
from .fleet import Fleet


async def initialize(
    directory: Path, username: str = "admin", *, password: str | None = None
) -> Path:
    control = ControlStore(directory)
    await control.open()
    try:
        password = password or secrets.token_urlsafe(24)
        await Auth(control).provision(username, password)
        destination = directory / "initial-account.txt"
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(
                f"NewAPI Watch Console\n用户名：{username}\n初始密码：{password}\n请妥善保管此文件，不要发送到群聊或提交仓库。\n"
            )
        return destination
    finally:
        await control.close()


async def import_legacy(directory: Path) -> str:
    from .config import Settings

    settings = Settings()
    if settings.demo:
        raise ControlError(400, "不能把DEMO环境作为真实平台导入")
    control = ControlStore(directory)
    await control.open()
    try:
        # Explicit import never enables the platform or connects its existing bot.
        for item in await control.platforms():
            if item["newapi_url"] == settings.newapi_url:
                raise ControlError(409, "该地址已导入，请在管理台编辑已有平台")
        platform = await control.create_platform(
            PlatformInput(
                name=settings.platform_name,
                newapi_url=settings.newapi_url,
                newapi_token=settings.newapi_token,
                newapi_user_id=settings.newapi_user_id,
                enabled=False,
                timezone=settings.timezone,
                poll_seconds=max(10, settings.poll_seconds),
                log_seconds=max(30, settings.log_seconds),
                llm=LLMConfig(
                    enabled=settings.llm_enabled,
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                ),
            )
        )
        if settings.group_webhook.get_secret_value():
            await control.create_binding(
                platform["id"],
                BindingInput(name="原有群告警", kind="webhook", webhook_url=settings.group_webhook),
            )
        if settings.bot_id:
            await control.create_binding(
                platform["id"],
                BindingInput(
                    name="原有智能机器人",
                    kind="bot",
                    bot_id=settings.bot_id,
                    bot_secret=settings.bot_secret,
                ),
            )
        return str(platform["id"])
    finally:
        await control.close()


async def seed_demo(control: ControlStore) -> None:
    if await control.platforms():
        return
    for name, url in (
        ("生产中转站", "https://production.demo.invalid"),
        ("备用中转站", "https://backup.demo.invalid"),
    ):
        platform = await control.create_platform(
            PlatformInput(
                name=name,
                newapi_url=url,
                newapi_token=SecretStr("demo-no-real-token"),
                newapi_user_id=1,
                enabled=True,
            )
        )
        await control.create_binding(
            platform["id"],
            BindingInput(
                name="演示告警群",
                kind="webhook",
                webhook_url=SecretStr(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=demo-not-real"
                ),
            ),
        )


def serve_console(
    directory: Path, *, host: str, port: int, demo: bool = False, secure_cookie: bool = True
) -> None:
    control = ControlStore(directory)
    fleet = Fleet(control, demo=demo)
    auth = Auth(control, secure_cookie=secure_cookie)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await control.open()
            if demo:
                await seed_demo(control)
            await fleet.open()
            yield
        finally:
            await fleet.close()
            await control.close()

    app = create_console(control, fleet, auth, lifespan=lifespan)
    uvicorn.run(app, host=host, port=port, workers=1, access_log=False)
