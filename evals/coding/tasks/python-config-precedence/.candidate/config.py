from defaults import DEFAULT_REGION, DEFAULT_RETRIES, DEFAULT_TIMEOUT

DEFAULTS: dict[str, object] = {
    "timeout": DEFAULT_TIMEOUT,
    "retries": DEFAULT_RETRIES,
    "region": DEFAULT_REGION,
}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    """Return the effective settings: defaults overridden by the caller."""
    return {**DEFAULTS, **overrides}
