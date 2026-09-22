from shortname import shorten


def test_short_name_is_unchanged() -> None:
    assert shorten("Build Agent") == "build-agent"
