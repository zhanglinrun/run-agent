from slug import slugify


def test_punctuation_becomes_one_separator() -> None:
    assert slugify("Hello, Agent!") == "hello-agent"


def test_existing_separator_runs_collapse() -> None:
    assert slugify("a___b---c") == "a-b-c"


def test_edge_separators_are_removed() -> None:
    assert slugify("--- Reliable build ---") == "reliable-build"


def test_empty_value_stays_empty() -> None:
    assert slugify(" !!! ") == ""
