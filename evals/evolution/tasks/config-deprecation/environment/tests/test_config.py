from config import resolve


def test_canonical_key() -> None:
    assert resolve({"base_url": "https://api.example"})["base_url"] == "https://api.example"
