"""Per-source CNY display values. Raw quota stays integer and remains authoritative."""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CurrencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False
    quota_per_unit: Decimal = Field(
        default=Decimal("500000"),
        gt=0,
        le=Decimal("1e18"),
        allow_inf_nan=False,
        max_digits=30,
        decimal_places=12,
    )
    usd_exchange_rate: Decimal = Field(
        default=Decimal("1"),
        gt=0,
        le=Decimal("1e12"),
        allow_inf_nan=False,
        max_digits=25,
        decimal_places=12,
    )
    source: Literal["manual", "status"] = "manual"

    @model_validator(mode="after")
    def explicit_parameters(self) -> Self:
        if self.confirmed and not {"quota_per_unit", "usd_exchange_rate"}.issubset(
            self.model_fields_set
        ):
            raise ValueError("确认人民币显示需要提供两项实际参数，不能猜测默认倍率")
        return self

    def amount(self, quota: int, *, precision: int = 2) -> str | None:
        if not self.confirmed:
            return None
        with localcontext() as ctx:
            ctx.prec = 70
            value = Decimal(quota) * self.usd_exchange_rate / self.quota_per_unit
            return format(
                value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP), "f"
            )

    def display(self, quota: int) -> str:
        amount = self.amount(quota)
        if amount is None:
            return f"{quota:,} quota（人民币换算未配置）"
        if Decimal(amount) == 0 and quota != 0:
            return "¥-<0.01" if quota < 0 else "¥<0.01"
        return "¥" + format(Decimal(amount), ",.2f")

    def to_quota(self, amount: str | Decimal) -> int:
        if not self.confirmed:
            raise ValueError("请先确认该数据源的人民币换算参数")
        if not isinstance(amount, str | Decimal):
            raise ValueError("人民币阈值必须是十进制字符串")
        try:
            value = Decimal(amount)
            if not value.is_finite() or value < 0 or value > Decimal("1e18"):
                raise ValueError("人民币阈值无效或超出范围")
            with localcontext() as ctx:
                ctx.prec = 70
                result = int(
                    (value * self.quota_per_unit / self.usd_exchange_rate).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
            if result > 10**18:
                raise ValueError("换算后的quota阈值超出范围")
            return result
        except (InvalidOperation, TypeError, OverflowError):
            raise ValueError("人民币阈值无效") from None


def currency_candidate(data: dict[str, Any]) -> CurrencyConfig:
    if not isinstance(data, dict):
        raise ValueError("平台状态接口结构无效")
    kind = data.get("quota_display_type")
    if kind != "CNY":
        raise ValueError("平台未声明CNY显示，不能仅根据¥符号猜测换算参数，请手工核对")
    if "quota_per_unit" not in data or "usd_exchange_rate" not in data:
        raise ValueError("平台缺少额度单位或人民币显示汇率，请手工核对")
    return CurrencyConfig(
        confirmed=True,
        quota_per_unit=data["quota_per_unit"],
        usd_exchange_rate=data["usd_exchange_rate"],
        source="status",
    )


def decorate_money(value: Any, currency: CurrencyConfig) -> Any:
    """Add Decimal-derived strings without changing API quota fields or supplier balance."""
    if isinstance(value, list):
        return [decorate_money(item, currency) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: decorate_money(item, currency) for key, item in value.items()}
    for key in ("quota", "used_quota", "threshold", "recovery_threshold"):
        if type(value.get(key)) is int:
            result[key + "_display"] = currency.display(value[key])
            result[key + "_cny"] = currency.amount(value[key], precision=12)
    return result


def normalize_balance_amounts(payload: dict[str, Any], currency: CurrencyConfig) -> dict[str, Any]:
    """CNY edits are explicit; a read/save without edits preserves original integer thresholds."""
    result = dict(payload)
    edits = result.pop("balance_amounts", {})
    if not isinstance(edits, dict):
        raise ValueError("余额金额编辑格式无效")
    balances = result.get("balances", [])
    if not isinstance(balances, list):
        raise ValueError("余额策略格式无效")
    updated = []
    found = set()
    for item in balances:
        if not isinstance(item, dict):
            raise ValueError("余额策略格式无效")
        rule = {
            k: v
            for k, v in item.items()
            if k
            not in {
                "threshold_display",
                "threshold_cny",
                "recovery_threshold_display",
                "recovery_threshold_cny",
            }
        }
        key = str(rule.get("user_id"))
        if key in edits:
            edit = edits[key]
            if not isinstance(edit, dict) or set(edit) - {
                "threshold_cny",
                "recovery_threshold_cny",
            }:
                raise ValueError("人民币阈值编辑字段无效")
            for name in ("threshold", "recovery_threshold"):
                if name + "_cny" in edit:
                    amount = edit[name + "_cny"]
                    rule[name] = (
                        None
                        if name == "recovery_threshold" and amount is None
                        else currency.to_quota(amount)
                    )
            found.add(key)
        updated.append(rule)
    if set(edits) != found:
        raise ValueError("金额编辑不属于当前用户策略")
    result["balances"] = updated
    return result
