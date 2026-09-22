DEFAULT_URL = "https://local.invalid"


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    value = overrides.get("endpoint", overrides.get("base_url", DEFAULT_URL))
    return {"base_url": value}
