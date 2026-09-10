from range_sum import range_sum


def test_inclusive_range() -> None:
    assert range_sum(1, 3) == 6


def test_single_value() -> None:
    assert range_sum(4, 4) == 4


def test_empty_range_is_zero() -> None:
    assert range_sum(3, 1) == 0


def test_negative_bounds() -> None:
    assert range_sum(-2, 2) == 0


def test_larger_range() -> None:
    assert range_sum(1, 100) == 5050
