from copy import deepcopy

DEFAULTS: dict[str, object] = {
    "mode": "dev",
    "service": {"host": "localhost", "port": 8000},
    "logging": {"level": "INFO", "json": False},
}


def _merge(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    result = deepcopy(base)
    for key, value in overrides.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return _merge(DEFAULTS, overrides)
