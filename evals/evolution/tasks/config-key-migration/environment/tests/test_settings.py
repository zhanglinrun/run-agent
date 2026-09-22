from settings import resolve


def test_retry_override_is_unchanged() -> None:
    assert resolve({"retries": 5})["retries"] == 5
