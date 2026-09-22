from unicode_name import normalize_name


def test_ascii_is_lowercased() -> None:
    assert normalize_name("Agent") == "agent"
