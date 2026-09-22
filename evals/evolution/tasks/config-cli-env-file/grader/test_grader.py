from cli_config import resolve


def test_cli_overrides_environment_and_file() -> None:
    result = resolve(
        ["--port", "9300", "--debug"],
        {"APP_PORT": "9200", "APP_DEBUG": "false"},
        {"port": 9100, "debug": False},
    )
    assert result == {"port": 9300, "debug": True}


def test_environment_overrides_file_with_types() -> None:
    result = resolve([], {"APP_PORT": "9200", "APP_DEBUG": "off"}, {"port": 9100, "debug": True})
    assert result == {"port": 9200, "debug": False}


def test_cli_can_disable_environment_flag() -> None:
    assert resolve(["--no-debug"], {"APP_DEBUG": "yes"}, {})["debug"] is False


def test_invalid_environment_boolean_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="APP_DEBUG"):
        resolve([], {"APP_DEBUG": "sometimes"}, {})
