from config import resolve


def test_file_value_overrides_default() -> None:
    assert resolve({"region": "eu"}, {})["region"] == "eu"
