DEFAULTS: dict[str, object] = {"port": 8000, "debug": False}


def resolve(
    cli: list[str], env: dict[str, str], file_values: dict[str, object]
) -> dict[str, object]:
    result = {**DEFAULTS, **file_values}
    if "APP_PORT" in env:
        result["port"] = env["APP_PORT"]
    if "APP_DEBUG" in env:
        result["debug"] = bool(env["APP_DEBUG"])
    return result
