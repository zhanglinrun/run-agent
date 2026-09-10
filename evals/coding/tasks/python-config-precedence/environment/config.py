from defaults import DEFAULT_REGION, DEFAULT_RETRIES, DEFAULT_TIMEOUT


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    """Return the effective settings: defaults overridden by the caller."""
    return {
        "timeout": overrides.get("timeout", 0),
        "retries": overrides.get("retries", 0),
        "region": overrides.get("region"),
    }


DEFAULTS = {
    "timeout": DEFAULT_TIMEOUT,
    "retries": DEFAULT_RETRIES,
    "region": DEFAULT_REGION,
}
