from range_sum import range_sum


def test_inclusive_range() -> None:
    assert range_sum(1, 3) == 6


def test_single_value() -> None:
    assert range_sum(4, 4) == 4
