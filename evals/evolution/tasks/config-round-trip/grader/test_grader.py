import pytest

from codec import dumps, loads


def test_mixed_types_round_trip() -> None:
    value = {"enabled": False, "limit": 0, "note": None, "ratio": 1.5}
    assert loads(dumps(value)) == value


def test_nested_values_round_trip() -> None:
    value = {"labels": ["alpha", "beta"], "meta": {"city": "Muenchen", "ok": True}}
    assert loads(dumps(value)) == value


def test_output_is_canonical_and_compact() -> None:
    assert dumps({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert "cafe" in dumps({"name": "cafe"})


def test_non_object_top_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="object"):
        loads('[1, 2]')
