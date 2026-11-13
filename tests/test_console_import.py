from newapi_watch.config import Settings
from newapi_watch.console import import_legacy
from newapi_watch.control_store import ControlStore


async def test_env_import_preserves_model_config_but_does_not_enable_platform(
    tmp_path, monkeypatch
):
    cfg = Settings(
        _env_file=None,
        demo=False,
        newapi_url="https://relay.example",
        newapi_token="management-key",
        newapi_user_id=2,
        api_key="service-key-only-for-tests-1234",
        llm_enabled=True,
        llm_base_url="https://model.example/v1",
        llm_api_key="model-only-key",
        llm_model="tool-model",
        platform_name="我的平台",
    )
    monkeypatch.setattr("newapi_watch.config.Settings", lambda: cfg)
    directory = tmp_path / "control"
    identifier = await import_legacy(directory)
    control = ControlStore(directory)
    await control.open()
    try:
        actual = await control.platform_config(identifier)
        assert actual.enabled is False and actual.name == "我的平台"
        assert actual.llm.enabled and actual.llm.api_key.get_secret_value() == "model-only-key"
        assert actual.llm.base_url == "https://model.example/v1"
    finally:
        await control.close()
