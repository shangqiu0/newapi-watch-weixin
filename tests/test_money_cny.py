from decimal import Decimal

import pytest
from pydantic import ValidationError

from newapi_watch.money import (
    CurrencyConfig,
    currency_candidate,
    decorate_money,
    normalize_balance_amounts,
)
from newapi_watch.monitoring.models import MonitoringConfig


def currency():
    return CurrencyConfig(confirmed=True, quota_per_unit="500000", usd_exchange_rate="7.3")


def test_unknown_currency_never_labels_quota_as_yuan():
    assert CurrencyConfig().display(500000) == "500,000 quota（人民币换算未配置）"
    assert CurrencyConfig().amount(500000) is None
    with pytest.raises(ValueError):
        CurrencyConfig().to_quota("10")


def test_cny_display_matches_platform_quota_without_float():
    rate = currency()
    assert rate.amount(500000) == "7.30"
    assert rate.display(66062545205) == "¥964,513.16"
    assert rate.amount(1, precision=6) == "0.000015"
    assert rate.display(1) == "¥<0.01"
    assert rate.display(-1) == "¥-<0.01"


def test_threshold_ceiling_preserves_strict_balance_boundary():
    rate = currency()
    threshold = rate.to_quota("100.01")
    assert threshold == 6850000
    assert Decimal(threshold - 1) * Decimal("7.3") / Decimal("500000") < Decimal("100.01")
    assert Decimal(threshold) * Decimal("7.3") / Decimal("500000") >= Decimal("100.01")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e40"])
def test_invalid_currency_parameters_rejected(value):
    with pytest.raises(ValidationError):
        CurrencyConfig(confirmed=True, usd_exchange_rate=value)


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1e40"])
def test_invalid_yuan_threshold_rejected(value):
    with pytest.raises(ValueError):
        currency().to_quota(value)


def test_currency_detection_requires_actual_cny_display_parameters():
    result = currency_candidate(
        {"quota_per_unit": 500000, "usd_exchange_rate": 7.3, "quota_display_type": "CNY"}
    )
    assert result.confirmed and result.amount(500000) == "7.30"
    for data in (
        {},
        {"quota_display_type": "USD"},
        {"quota_display_type": "CNY", "quota_per_unit": 500000},
    ):
        with pytest.raises(ValueError):
            currency_candidate(data)


def test_annotations_keep_raw_numbers_and_do_not_convert_supplier_balance():
    raw = {"quota": 500000, "used_quota": 1000000, "balance": 123, "items": [{"quota": 1}]}
    result = decorate_money(raw, currency())
    assert result["quota"] == 500000 and result["quota_display"] == "¥7.30"
    assert result["used_quota_display"] == "¥14.60"
    assert result["items"][0]["quota_display"] == "¥<0.01"
    assert "balance_display" not in result and "quota_display" not in raw


def test_monitoring_threshold_exact_text_survives_json_number_precision():
    threshold = 9_007_199_254_740_993
    raw = {
        "balances": [{"user_id": 29, "threshold": threshold, "recovery_threshold": threshold + 2}]
    }
    response = decorate_money(raw, currency())
    rule = response["balances"][0]
    assert rule["threshold_quota"] == "9007199254740993"
    assert rule["recovery_threshold_quota"] == "9007199254740995"
    rule["threshold"] = rule["threshold_quota"]
    rule["recovery_threshold"] = rule["recovery_threshold_quota"]
    normalized = MonitoringConfig.model_validate(normalize_balance_amounts(response, currency()))
    assert normalized.balances[0].threshold == threshold
    assert normalized.balances[0].recovery_threshold == threshold + 2
