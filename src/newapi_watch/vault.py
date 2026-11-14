"""Authenticated encryption for control-plane credentials, with a separate local root key."""

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, SecretStr


def plaintext(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return {name: plaintext(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, dict):
        return {key: plaintext(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plaintext(item) for item in value]
    return value


class Vault:
    def __init__(self, path: Path, *, allow_create: bool) -> None:
        if not path.exists():
            if not allow_create:
                raise RuntimeError("控制库已存在但主密钥丢失，拒绝生成新密钥；请恢复备份")
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
        try:
            self.cipher = Fernet(path.read_bytes())
        except (ValueError, OSError):
            raise RuntimeError("无法读取控制库主密钥") from None

    def seal(self, config: Any) -> str:
        return self.cipher.encrypt(
            json.dumps(plaintext(config), ensure_ascii=False).encode()
        ).decode()

    def open(self, value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(self.cipher.decrypt(value.encode()))
            if not isinstance(decoded, dict):
                raise ValueError("invalid config")
            return decoded
        except (InvalidToken, ValueError):
            raise RuntimeError("控制库凭证解密失败，请核对主密钥与备份") from None
