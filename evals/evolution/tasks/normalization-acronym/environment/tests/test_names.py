from names import to_snake


def test_simple_camel_case() -> None:
    assert to_snake("userName") == "user_name"
