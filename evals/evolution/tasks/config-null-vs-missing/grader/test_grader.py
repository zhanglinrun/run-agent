from config import resolve


def test_missing_values_use_defaults() -> None:
    assert resolve({}) == {"label": "worker", "timeout": 30}


def test_explicit_null_is_preserved() -> None:
    assert resolve({"label": None})["label"] is None


def test_empty_string_is_preserved() -> None:
    assert resolve({"label": ""})["label"] == ""


def test_zero_is_preserved() -> None:
    assert resolve({"timeout": 0})["timeout"] == 0
