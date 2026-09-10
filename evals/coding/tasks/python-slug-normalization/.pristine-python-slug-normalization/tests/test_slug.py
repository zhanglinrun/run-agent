from slug import slugify


def test_collapses_separators() -> None:
    assert slugify("  Hello__  Agent  ") == "hello-agent"


def test_removes_edge_separators() -> None:
    assert slugify("__Reliable Harness__") == "reliable-harness"
