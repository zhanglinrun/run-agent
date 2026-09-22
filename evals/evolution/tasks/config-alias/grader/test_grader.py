from config import resolve


def test_legacy_alias_is_accepted() -> None:
    assert resolve({"colour": "blue"}) == {"color": "blue", "size": "m"}


def test_canonical_key_wins_and_alias_is_removed() -> None:
    assert resolve({"color": "black", "colour": "white"}) == {
        "color": "black",
        "size": "m",
    }


def test_other_options_are_preserved() -> None:
    assert resolve({"size": "xl", "contrast": "high"}) == {
        "color": "red",
        "size": "xl",
        "contrast": "high",
    }
