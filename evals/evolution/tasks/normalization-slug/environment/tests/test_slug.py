from slug import slugify


def test_words_are_lowercase_and_joined() -> None:
    assert slugify("Hello World") == "hello-world"
