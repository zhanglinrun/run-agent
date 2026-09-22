from config import resolve


def test_canonical_override() -> None:
    assert resolve({"color": "green"})["color"] == "green"
