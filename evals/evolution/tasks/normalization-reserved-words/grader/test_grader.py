from identifiers import to_identifier


def test_class_keyword_gets_suffix() -> None:
    assert to_identifier("class") == "class_"


def test_for_keyword_gets_suffix() -> None:
    assert to_identifier("for") == "for_"


def test_yield_keyword_gets_suffix() -> None:
    assert to_identifier("yield") == "yield_"


def test_leading_digit_still_gets_prefix() -> None:
    assert to_identifier("2 workers") == "_2_workers"
