import re

import pytest

from shortname import shorten


def test_equal_prefixes_do_not_collide() -> None:
    first = shorten("customer-account-production-east")
    second = shorten("customer-account-production-west")
    assert first != second
    assert len(first) <= 24
    assert len(second) <= 24


def test_long_name_has_hash_suffix() -> None:
    result = shorten("a very long deployment name for testing")
    assert len(result) <= 24
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8}", result)


def test_too_small_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 10"):
        shorten("anything", max_length=9)


def test_result_is_deterministic() -> None:
    assert shorten("same long deployment identifier") == shorten("same long deployment identifier")
