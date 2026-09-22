from keycodec import decode, encode


def test_delimiter_inputs_do_not_collide() -> None:
    assert encode(["a|b", "c"]) != encode(["a", "b|c"])


def test_delimiter_values_round_trip() -> None:
    parts = ["a|b", "c:d", "tail"]
    assert decode(encode(parts)) == parts


def test_empty_and_delimiter_values_round_trip() -> None:
    parts = ["", "|", ""]
    assert decode(encode(parts)) == parts


def test_empty_list_round_trip() -> None:
    assert decode(encode([])) == []
