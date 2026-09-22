from pathnorm import normalize_segment


def test_parent_segment_is_neutralized() -> None:
    assert normalize_segment("..") == "_"


def test_backslash_is_not_a_separator() -> None:
    assert normalize_segment("logs\\2025") == "logs_2025"


def test_windows_device_name_is_prefixed() -> None:
    assert normalize_segment("con") == "_con"
    assert normalize_segment("LPT9.txt") == "_LPT9.txt"


def test_trailing_dots_and_spaces_are_removed() -> None:
    assert normalize_segment(" report... ") == "report"


def test_regular_segment_is_unchanged() -> None:
    assert normalize_segment("report-2025.txt") == "report-2025.txt"
