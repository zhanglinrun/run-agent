from config import DEFAULTS, resolve


def test_file_overrides_default() -> None:
    assert resolve({"timeout": 12}, {})["timeout"] == 12


def test_environment_overrides_file() -> None:
    assert resolve({"region": "file"}, {"region": "env"})["region"] == "env"


def test_falsey_environment_value_overrides_file() -> None:
    assert resolve({"timeout": 9}, {"timeout": 0})["timeout"] == 0


def test_inputs_are_not_mutated() -> None:
    file_values = {"region": "file"}
    env = {"timeout": 8}
    resolve(file_values, env)
    assert file_values == {"region": "file"}
    assert env == {"timeout": 8}
    assert DEFAULTS == {"region": "local", "timeout": 30}
