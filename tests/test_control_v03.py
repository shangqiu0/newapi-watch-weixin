import json

import pytest

from newapi_watch.auth import Auth
from newapi_watch.control_models import BindingInput, PlatformInput, PlatformPatch
from newapi_watch.control_store import ControlError, ControlStore


@pytest.fixture
async def control(tmp_path):
    store = ControlStore(tmp_path / "control")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def platform(name="A"):
    return PlatformInput(
        name=name,
        newapi_url="https://relay.example",
        newapi_token="admin-token-secret",
        newapi_user_id=2,
    )


async def test_control_encrypts_credentials_and_returns_only_presence(control):
    saved = await control.create_platform(platform())
    assert saved["name"] == "A" and saved["has_newapi_token"]
    assert "admin-token-secret" not in json.dumps(saved)
    private = await control.platform_config(saved["id"])
    assert private.newapi_token.get_secret_value() == "admin-token-secret"
    async with control.transaction() as db:
        cursor = await db.execute("SELECT config FROM platforms WHERE id=?", (saved["id"],))
        assert "admin-token-secret" not in (await cursor.fetchone())[0]
    await control.patch_platform(saved["id"], PlatformPatch(name="renamed", newapi_token=""))
    assert (
        await control.platform_config(saved["id"])
    ).newapi_token.get_secret_value() == "admin-token-secret"


async def test_binding_is_platform_scoped_and_bot_unique(control):
    a = await control.create_platform(platform("A"))
    b = await control.create_platform(platform("B"))
    bot = BindingInput(name="bot A", kind="bot", bot_id="unique-bot", bot_secret="bot-secret")
    binding = await control.create_binding(a["id"], bot)
    with pytest.raises(ControlError) as cross:
        await control.binding(b["id"], binding["id"])
    assert cross.value.status == 404
    with pytest.raises(ControlError) as duplicate:
        await control.create_binding(b["id"], bot)
    assert duplicate.value.status == 409
    assert "bot-secret" not in json.dumps(await control.bindings(a["id"]))


async def test_identity_url_cannot_be_silently_rebound(control):
    a = await control.create_platform(platform())
    with pytest.raises(ControlError):
        await control.patch_platform(a["id"], PlatformPatch(newapi_url="https://different.example"))
    with pytest.raises(ControlError):
        await control.platform_config("../other-platform")


async def test_auth_login_csrf_expiration_and_revocation(control):
    auth = Auth(control, secure_cookie=False)
    await auth.provision("operator", "a-strong-password-only-for-tests")
    with pytest.raises(ControlError) as bad:
        await auth.login("operator", "wrong", "127.0.0.1")
    assert bad.value.status == 401
    session = await auth.login("operator", "a-strong-password-only-for-tests", "127.0.0.1")
    assert (await auth.authenticate(session.token))["username"] == "operator"
    await auth.check_csrf(session.token, session.csrf)
    with pytest.raises(ControlError):
        await auth.check_csrf(session.token, "wrong")
    await auth.logout(session.token)
    with pytest.raises(ControlError):
        await auth.authenticate(session.token)
    async with control.transaction() as db:
        cursor = await db.execute("SELECT password_hash FROM accounts")
        encoded = (await cursor.fetchone())[0]
        assert "a-strong-password" not in encoded and encoded.startswith("scrypt$")


async def test_login_rate_limit_and_no_account_replacement(control):
    auth = Auth(control, secure_cookie=False)
    await auth.provision("admin", "safe-test-passphrase-123456")
    with pytest.raises(ControlError):
        await auth.provision("admin", "different-passphrase-123456")
    for _ in range(6):
        with pytest.raises(ControlError):
            await auth.login("admin", "incorrect", "local-test")
    with pytest.raises(ControlError) as limited:
        await auth.login("admin", "safe-test-passphrase-123456", "local-test")
    assert limited.value.status == 429


async def test_master_key_missing_does_not_generate_new_key_over_existing_data(tmp_path):
    store = ControlStore(tmp_path / "control")
    await store.open()
    await store.create_platform(platform())
    await store.close()
    store.key_path.unlink()
    with pytest.raises(RuntimeError):
        await ControlStore(tmp_path / "control").open()


async def test_control_future_schema_is_not_downgraded(tmp_path):
    import sqlite3
    from contextlib import closing

    directory = tmp_path / "future"
    current = ControlStore(directory)
    await current.open()
    await current.close()
    with closing(sqlite3.connect(current.path)) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        await ControlStore(directory).open()
    with closing(sqlite3.connect(current.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


async def test_expired_session_is_denied(control):
    auth = Auth(control, secure_cookie=False)
    await auth.provision("admin", "safe-passphrase-test-1234")
    session = await auth.login("admin", "safe-passphrase-test-1234", "test")
    async with control.transaction() as db:
        await db.execute(
            "UPDATE sessions SET expires_at=1 WHERE id=?",
            (__import__("hashlib").sha256(session.token.encode()).hexdigest(),),
        )
    with pytest.raises(ControlError) as expired:
        await auth.authenticate(session.token)
    assert expired.value.status == 401
