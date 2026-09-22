from codec import dumps, loads


def test_string_values_round_trip() -> None:
    value = {"name": "agent"}
    assert loads(dumps(value)) == value
