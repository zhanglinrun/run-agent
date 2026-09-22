from textnorm import normalize_whitespace


def test_two_spaces_collapse() -> None:
    assert normalize_whitespace("alpha  beta") == "alpha beta"
