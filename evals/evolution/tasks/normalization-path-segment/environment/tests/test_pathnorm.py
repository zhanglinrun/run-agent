from pathnorm import normalize_segment


def test_forward_slash_is_replaced() -> None:
    assert normalize_segment("logs/2025") == "logs_2025"
