import client
import settings


def test_default_uses_only_new_schema() -> None:
    assert settings.resolve({}) == {
        "base_url": "https://local.invalid",
        "retries": 2,
    }


def test_removed_key_is_ignored() -> None:
    assert settings.resolve({"api_host": "https://old.invalid"}) == {
        "base_url": "https://local.invalid",
        "retries": 2,
    }


def test_client_uses_new_key() -> None:
    assert client.endpoint({"base_url": "https://api.example"}) == "https://api.example/v1"


def test_retry_override_survives_migration() -> None:
    assert settings.resolve({"retries": 7})["retries"] == 7
