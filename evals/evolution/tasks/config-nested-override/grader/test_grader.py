from config import DEFAULTS, resolve


def test_partial_service_override_keeps_sibling() -> None:
    assert resolve({"service": {"port": 9000}})["service"] == {
        "host": "localhost",
        "port": 9000,
    }


def test_partial_logging_override_keeps_sibling() -> None:
    assert resolve({"logging": {"json": True}})["logging"] == {
        "level": "INFO",
        "json": True,
    }


def test_unknown_nested_keys_are_kept() -> None:
    assert resolve({"service": {"workers": 4}})["service"]["workers"] == 4


def test_results_do_not_share_nested_defaults() -> None:
    result = resolve({})
    result["service"]["host"] = "changed"
    assert DEFAULTS["service"] == {"host": "localhost", "port": 8000}
    assert resolve({})["service"]["host"] == "localhost"
