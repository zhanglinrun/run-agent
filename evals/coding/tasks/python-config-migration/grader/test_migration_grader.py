import client
import settings


def test_override_is_honoured_in_milliseconds() -> None:
    assert settings.resolve({"timeout_ms": 5000})["timeout_ms"] == 5000


def test_the_default_is_thirty_thousand_milliseconds() -> None:
    assert settings.resolve({})["timeout_ms"] == 30000


def test_the_old_key_is_gone() -> None:
    assert "timeout_seconds" not in settings.resolve({})
    assert "timeout_seconds" not in settings.resolve({"timeout_seconds": 5})


def test_the_client_migrated_with_the_settings() -> None:
    assert client.build({"timeout_ms": 5000}) == "timeout=5000ms"


def test_the_defaults_module_still_exports_the_value() -> None:
    import defaults

    assert defaults.DEFAULT_TIMEOUT_MS == 30000
