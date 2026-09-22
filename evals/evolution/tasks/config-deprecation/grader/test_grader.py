import warnings

import pytest

from config import resolve


def test_deprecated_key_emits_warning() -> None:
    with pytest.warns(DeprecationWarning, match="endpoint.*base_url") as captured:
        result = resolve({"endpoint": "https://old.example"})
    assert result == {"base_url": "https://old.example"}
    assert len(captured) == 1


def test_canonical_key_wins_when_both_are_present() -> None:
    with pytest.warns(DeprecationWarning) as captured:
        result = resolve({
            "endpoint": "https://old.example",
            "base_url": "https://new.example",
        })
    assert result == {"base_url": "https://new.example"}
    assert len(captured) == 1


def test_canonical_key_does_not_warn() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        assert resolve({"base_url": "https://new.example"}) == {
            "base_url": "https://new.example"
        }
    assert captured == []
