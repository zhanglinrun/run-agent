from config import resolve


def test_regular_values_override_defaults() -> None:
    assert resolve({"label": "api", "timeout": 5}) == {"label": "api", "timeout": 5}
