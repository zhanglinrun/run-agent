from slug import slugify


def test_collapses_separators() -> None:
    assert slugify("  Hello__  Agent  ") == "hello-agent"


def test_removes_edge_separators() -> None:
    assert slugify("__Reliable Harness__") == "reliable-harness"


def test_mixed_separator_runs() -> None:
    assert slugify("a _ b__c") == "a-b-c"


def test_empty_input() -> None:
    assert slugify("   ") == ""


def test_already_normalized_is_unchanged() -> None:
    assert slugify("plain-slug") == "plain-slug"
