from config import DEFAULTS, resolve


def test_plugins_are_a_stable_union() -> None:
    result = resolve(
        {"plugins": ["logging", "cache"]},
        {"plugins": ["debug", "core"]},
    )
    assert result["plugins"] == ["core", "logging", "cache", "debug"]


def test_plugins_merge_when_runtime_is_missing() -> None:
    assert resolve({"plugins": ["cache"]}, {})["plugins"] == [
        "core",
        "logging",
        "cache",
    ]


def test_duplicate_plugins_are_removed() -> None:
    assert resolve({}, {"plugins": ["core", "core", "audit"]})["plugins"] == [
        "core",
        "logging",
        "audit",
    ]


def test_inputs_and_defaults_are_not_mutated() -> None:
    file_values = {"plugins": ["cache"]}
    overrides = {"plugins": ["audit"]}
    resolve(file_values, overrides)
    assert file_values == {"plugins": ["cache"]}
    assert overrides == {"plugins": ["audit"]}
    assert DEFAULTS["plugins"] == ["core", "logging"]
