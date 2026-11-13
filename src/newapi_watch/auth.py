"""Single pre-provisioned administrator, revocable sessions and bounded login attempts."""

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from .control_store import ControlError, ControlStore
from .persistence import row


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${derived.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, salt, _ = encoded.split("$")
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    token: str
    csrf: str
    username: str


class Auth:
    def __init__(self, control: ControlStore, *, secure_cookie: bool = True) -> None:
        self.control, self.secure_cookie = control, secure_cookie

    async def provision(self, username: str, password: str) -> None:
        if not 1 <= len(username) <= 80 or len(password) < 16 or len(password) > 256:
            raise ControlError(400, "账号或密码长度无效，密码至少16位")
        encoded = await asyncio.to_thread(password_hash, password)
        now = time.time()
        async with self.control.transaction() as db:
            if await row(db, "SELECT id FROM accounts LIMIT 1"):
                raise ControlError(409, "管理账号已初始化，不允许覆盖或注册")
            await db.execute(
                "INSERT INTO accounts(id,username,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
                (uuid4().hex, username, encoded, now, now),
            )

    async def login(self, username: str, password: str, address: str) -> Session:
        now, key = time.time(), digest(address)
        async with self.control.transaction() as db:
            attempt = await row(db, "SELECT count,until_at FROM login_attempts WHERE id=?", (key,))
            if attempt and attempt["until_at"] > now and attempt["count"] >= 6:
                raise ControlError(429, "尝试过于频繁，请15分钟后再试")
            await db.execute(
                """INSERT INTO login_attempts(id,count,until_at,created_at,updated_at) VALUES(?,1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET count=CASE WHEN until_at>? THEN count+1 ELSE 1 END,
                until_at=CASE WHEN until_at>? THEN until_at ELSE excluded.until_at END,updated_at=excluded.updated_at""",
                (key, now + 900, now, now, now, now),
            )
            account = await row(
                db, "SELECT username,password_hash FROM accounts WHERE username=?", (username,)
            )
        # Run one fixed-cost scrypt even for missing accounts; never echo credentials.
        encoded = (
            account["password_hash"]
            if account
            else password_hash("dummy-account-passphrase", b"0" * 16)
        )
        valid = await asyncio.to_thread(password_matches, password, encoded)
        if not account or not valid:
            raise ControlError(401, "账号或密码不正确")
        token = secrets.token_urlsafe(32)
        csrf = self.csrf(token)
        async with self.control.transaction() as db:
            await db.execute("DELETE FROM login_attempts WHERE id=?", (key,))
            await db.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
            await db.execute(
                "INSERT INTO sessions(id,username,csrf_hash,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (digest(token), account["username"], digest(csrf), now + 8 * 3600, now, now),
            )
        return Session(token, csrf, account["username"])

    async def authenticate(self, token: str) -> dict[str, str]:
        if not 16 <= len(token) <= 128:
            raise ControlError(401, "请先登录管理台")
        async with self.control.transaction() as db:
            session = await row(
                db,
                "SELECT username,csrf_hash,expires_at FROM sessions WHERE id=?",
                (digest(token),),
            )
        if not session or session["expires_at"] <= time.time():
            raise ControlError(401, "会话已过期，请重新登录")
        return {"username": session["username"], "csrf_hash": session["csrf_hash"]}

    async def check_csrf(self, token: str, csrf: str) -> None:
        session = await self.authenticate(token)
        if not hmac.compare_digest(session["csrf_hash"], digest(csrf)):
            raise ControlError(403, "请求验证失败，请刷新后重试")

    async def logout(self, token: str) -> None:
        async with self.control.transaction() as db:
            await db.execute("DELETE FROM sessions WHERE id=?", (digest(token),))

    def csrf(self, token: str) -> str:
        return hmac.new(
            self.control.key_path.read_bytes(), ("csrf:" + token).encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def session_fingerprint(token: str) -> str:
        return digest(token)
