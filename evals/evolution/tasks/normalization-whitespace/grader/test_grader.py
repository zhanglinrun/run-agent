from textnorm import normalize_whitespace


def test_long_ascii_run_collapses() -> None:
    assert normalize_whitespace("alpha    beta") == "alpha beta"


def test_tabs_and_newlines_collapse() -> None:
    assert normalize_whitespace(" alpha\t\n beta ") == "alpha beta"


def test_unicode_whitespace_collapses() -> None:
    assert normalize_whitespace("alpha\u00a0\u2003beta") == "alpha beta"


def test_whitespace_only_is_empty() -> None:
    assert normalize_whitespace(" \t\n ") == ""
