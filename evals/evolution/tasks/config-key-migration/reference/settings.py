DEFAULTS: dict[str, object] = {
    "base_url": "https://local.invalid",
    "retries": 2,
}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    result = dict(DEFAULTS)
    for key in ("base_url", "retries"):
        if key in overrides:
            result[key] = overrides[key]
    return result
