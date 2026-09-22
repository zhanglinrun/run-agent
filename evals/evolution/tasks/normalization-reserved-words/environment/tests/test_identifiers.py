from identifiers import to_identifier


def test_words_become_identifier() -> None:
    assert to_identifier("Hello world") == "hello_world"
