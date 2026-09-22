from unicode_name import normalize_name


def test_combining_form_is_normalized() -> None:
    assert normalize_name("Cafe\u0301") == "caf\u00e9"


def test_compatibility_characters_are_normalized() -> None:
    assert normalize_name("ＡＧＥＮＴ") == "agent"


def test_casefold_handles_sharp_s() -> None:
    assert normalize_name("Stra\u00dfe") == "strasse"


def test_already_normalized_value_is_unchanged() -> None:
    assert normalize_name("agent") == "agent"
