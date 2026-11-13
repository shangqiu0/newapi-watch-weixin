"""Redaction at output boundaries, covering every configured SecretStr field."""

from typing import Any

from pydantic import SecretStr

from .config import Settings


def redact_text(value: str, settings: Settings) -> str:
    secrets = [
        item.get_secret_value()
        for name in type(settings).model_fields
        if isinstance((item := getattr(settings, name)), SecretStr) and item.get_secret_value()
    ]
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def redact_data(value: Any, settings: Settings) -> Any:
    if isinstance(value, str):
        return redact_text(value, settings)
    if isinstance(value, dict):
        return {key: redact_data(item, settings) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item, settings) for item in value]
    return value
