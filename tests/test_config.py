import pytest
from pydantic import ValidationError

from newapi_watch.config import Settings


def test_production_requires_credentials_and_service_key():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, demo=False)


def test_valid_production_config_and_secrets_are_hidden():
    settings = Settings(
        _env_file=None,
        demo=False,
        newapi_url="https://relay.example",
        newapi_token="secret-value",
        newapi_user_id=1,
        api_key="a" * 32,
    )
    assert "secret-value" not in repr(settings)
    assert settings.timezone == "Asia/Shanghai"


@pytest.mark.parametrize(
    "values",
    [
        {"poll_seconds": 0},
        {"timezone": "Invalid/Nowhere"},
        {"bot_id": "only-id"},
        {"newapi_url": "file:///etc/passwd"},
        {"webhook_enabled": True},
        {"page_size": 1000},
    ],
)
def test_invalid_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, demo=True, **values)
