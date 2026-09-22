from cli_config import resolve


def test_file_values_override_defaults() -> None:
    assert resolve([], {}, {"port": 8100})["port"] == 8100
