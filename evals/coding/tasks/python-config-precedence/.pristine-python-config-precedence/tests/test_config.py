from config import resolve


def test_override_wins() -> None:
    assert resolve({"timeout": 5})["timeout"] == 5
