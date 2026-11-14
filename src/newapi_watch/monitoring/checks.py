"""State policies consume complete source responses, not a historical data warehouse."""

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..models import Channel
from ..persistence import rows
from ..redaction import redact_text
from .models import BalanceRule, ChannelRule
from .repository import PolicyRepository

if TYPE_CHECKING:
    from ..runtime import Runtime


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime(
        "%Y年%m月%d日 %H:%M:%S（北京时间）"
    )


class StateChecks:
    def __init__(self, runtime: "Runtime", repo: PolicyRepository) -> None:
        self.runtime, self.repo = runtime, repo

    async def channels(self, rule: ChannelRule, now: float) -> dict[str, int]:
        result = await self.runtime.commands.data.channels(force=True)
        channels = [Channel.model_validate(item) for item in result["items"]]
        selected = [c for c in channels if not rule.channel_ids or c.id in rule.channel_ids]
        # A single full latest snapshot supports both comparisons and manual inspection.
        async with self.runtime.store.transaction() as db:
            previous = {c["id"] for c in await rows(db, "SELECT id FROM channels")}
        for channel in selected:
            active = channel.status == 3 if channel.status in (1, 3) else None
            text = f"渠道{'自动禁用告警' if active else '恢复通知'}\n渠道：{channel.name}（#{channel.id}）\n当前状态：{channel.status}\n时间：{timestamp(now)}"
            await self.repo.condition(
                f"policy:channel:{channel.id}:status",
                active,
                redact_text(text, self.runtime.settings),
                rule,
                "channel",
                now,
                evidence={"channel_id": channel.id, "status": channel.status},
            )
            if rule.notify_missing:
                await self.repo.condition(
                    f"policy:channel:{channel.id}:missing",
                    False,
                    f"渠道 #{channel.id} 已重新出现在完整列表\n时间：{timestamp(now)}",
                    rule,
                    "channel",
                    now,
                )
        ids = {c.id for c in channels}
        missing = (set(rule.channel_ids) if rule.channel_ids else set(previous)) - ids
        if rule.notify_missing:
            for channel_id in missing:
                await self.repo.condition(
                    f"policy:channel:{channel_id}:missing",
                    True,
                    f"渠道缺失告警\n渠道：#{channel_id}\n已完整获取列表但未找到该渠道\n时间：{timestamp(now)}",
                    rule,
                    "channel",
                    now,
                )
        async with self.runtime.store.transaction() as db:
            await db.execute("UPDATE channels SET is_present=0 WHERE is_present=1")
            for c in channels:
                await db.execute(
                    """INSERT INTO channels(id,payload,status,is_present,created_at,updated_at) VALUES(?,?,?,1,?,?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status,is_present=1,updated_at=excluded.updated_at""",
                    (c.id, c.model_dump_json(), c.status, now, now),
                )
            await self.runtime.store.put_meta(db, "channels_last_ok", str(now), now)
        return {
            "objects": len(selected),
            "all_channels": len(channels),
            "auto_disabled": sum(c.status == 3 for c in selected),
        }

    async def balance(self, rule: BalanceRule, now: float) -> dict[str, int]:
        user = await self.runtime.client.user(rule.user_id)
        key = f"policy:balance:{rule.user_id}"
        async with self.runtime.store.transaction() as db:
            state = await rows(db, "SELECT is_active FROM monitor_states WHERE id=?", (key,))
        active = user.quota < rule.threshold
        if state and state[0]["is_active"] and rule.recovery_threshold is not None:
            active = user.quota < rule.recovery_threshold
        text = f"{'余额不足提醒' if active else '余额恢复通知'}\n用户 #{rule.user_id} · {rule.name or user.username}\n当前余额：{self.runtime.settings.currency.display(user.quota)}\n监控阈值：{self.runtime.settings.currency.display(rule.threshold)}\n时间：{timestamp(now)}"
        await self.repo.condition(
            key,
            active,
            redact_text(text, self.runtime.settings),
            rule,
            "balance",
            now,
            evidence={"quota": user.quota, "threshold": rule.threshold, "user_id": rule.user_id},
        )
        async with self.runtime.store.transaction() as db:
            await db.execute(
                """INSERT INTO account_samples(id,quota,threshold,is_low,created_at,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET quota=excluded.quota,threshold=excluded.threshold,is_low=excluded.is_low,updated_at=excluded.updated_at""",
                (rule.user_id, user.quota, rule.threshold, int(active), now, now),
            )
        await self.runtime.store.set_meta(f"balance_error:{rule.user_id}", "")
        return {"quota": user.quota, "threshold": rule.threshold}
