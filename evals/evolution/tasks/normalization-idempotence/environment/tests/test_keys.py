from keys import normalize_key


def test_raw_key_is_normalized() -> None:
    assert normalize_key("User_Name") == "key-user-name"
