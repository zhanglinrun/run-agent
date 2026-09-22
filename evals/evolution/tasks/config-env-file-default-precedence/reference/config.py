DEFAULTS: dict[str, object] = {"region": "local", "timeout": 30}


def resolve(
    file_values: dict[str, object], env: dict[str, object]
) -> dict[str, object]:
    return {**DEFAULTS, **file_values, **env}
