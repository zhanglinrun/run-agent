import argparse

DEFAULTS: dict[str, object] = {"port": 8000, "debug": False}


def _env_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid APP_DEBUG value: {value}")


def resolve(
    cli: list[str], env: dict[str, str], file_values: dict[str, object]
) -> dict[str, object]:
    result = {**DEFAULTS, **file_values}
    if "APP_PORT" in env:
        result["port"] = int(env["APP_PORT"])
    if "APP_DEBUG" in env:
        result["debug"] = _env_bool(env["APP_DEBUG"])

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--debug", dest="debug", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--no-debug", dest="debug", action="store_false", default=argparse.SUPPRESS)
    result.update(vars(parser.parse_args(cli)))
    return result
