from config import resolve


def test_override_wins() -> None:
    assert resolve({"timeout": 5})["timeout"] == 5


def test_missing_override_falls_back_to_default_timeout() -> None:
    assert resolve({})["timeout"] == 30


def test_missing_override_falls_back_to_default_retries() -> None:
    assert resolve({})["retries"] == 3


def test_missing_override_falls_back_to_default_region() -> None:
    assert resolve({})["region"] == "local"


def test_partial_override_keeps_the_other_defaults() -> None:
    resolved = resolve({"region": "eu"})
    assert resolved == {"timeout": 30, "retries": 3, "region": "eu"}
