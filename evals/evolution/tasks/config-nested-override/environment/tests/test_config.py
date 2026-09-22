from config import resolve


def test_top_level_scalar_override() -> None:
    assert resolve({"mode": "prod"})["mode"] == "prod"
