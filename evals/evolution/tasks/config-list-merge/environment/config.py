DEFAULTS: dict[str, object] = {"mode": "safe", "plugins": ["core", "logging"]}


def resolve(
    file_values: dict[str, object], overrides: dict[str, object]
) -> dict[str, object]:
    return {**DEFAULTS, **file_values, **overrides}
