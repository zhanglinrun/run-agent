DEFAULTS: dict[str, object] = {
    "api_host": "https://local.invalid",
    "retries": 2,
}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return {**DEFAULTS, **overrides}
