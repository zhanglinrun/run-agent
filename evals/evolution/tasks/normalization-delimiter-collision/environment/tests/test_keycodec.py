from keycodec import decode, encode


def test_simple_parts_round_trip() -> None:
    parts = ["alpha", "beta"]
    assert decode(encode(parts)) == parts
