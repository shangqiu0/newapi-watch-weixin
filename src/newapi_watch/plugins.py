"""Small explicit extension API, without executing arbitrary files from chat."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from .models import CommandResult

if TYPE_CHECKING:
    from .runtime import Runtime

CommandHandler = Callable[[str], Awaitable[CommandResult]]
Job = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Collector:
    interval: float
    callback: Job


class Registry:
    def __init__(self) -> None:
        self.commands: dict[str, CommandHandler] = {}
        self.collectors: dict[str, Collector] = {}

    def command(self, name: str, handler: CommandHandler) -> None:
        if not name or len(name) > 64 or " " in name or name in self.commands:
            raise ValueError("扩展命令名称不合法或重复")
        self.commands[name] = handler

    def collector(self, name: str, interval: float, callback: Job) -> None:
        if not name or name in self.collectors or interval < 1:
            raise ValueError("扩展采集任务重复或间隔无效")
        self.collectors[name] = Collector(interval, callback)

    def load(self, enabled: tuple[str, ...], runtime: "Runtime") -> None:
        available = {item.name: item for item in entry_points(group="newapi_watch.plugins")}
        for name in enabled:
            if name not in available:
                raise ValueError(f"扩展未安装：{name}")
            register = available[name].load()
            register(self, runtime)
