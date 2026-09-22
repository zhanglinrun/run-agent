DEFAULTS: dict[str, object] = {
    "mode": "dev",
    "service": {"host": "localhost", "port": 8000},
    "logging": {"level": "INFO", "json": False},
}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return {**DEFAULTS, **overrides}
