from keys import normalize_key


def test_already_normalized_key_is_unchanged() -> None:
    assert normalize_key("key-user-name") == "key-user-name"


def test_normalization_is_idempotent() -> None:
    once = normalize_key("Account__Owner")
    assert normalize_key(once) == once


def test_repeated_prefixes_collapse() -> None:
    assert normalize_key("key-key-key-build") == "key-build"


def test_empty_value_is_idempotent() -> None:
    assert normalize_key(normalize_key("")) == "key"
