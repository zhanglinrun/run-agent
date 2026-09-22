import warnings

DEFAULT_URL = "https://local.invalid"


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    if "endpoint" in overrides:
        warnings.warn(
            "endpoint is deprecated; use base_url",
            DeprecationWarning,
            stacklevel=2,
        )
    if "base_url" in overrides:
        value = overrides["base_url"]
    elif "endpoint" in overrides:
        value = overrides["endpoint"]
    else:
        value = DEFAULT_URL
    return {"base_url": value}
