from defaults import DEFAULT_TIMEOUT_SECONDS


def resolve(overrides: dict[str, int]) -> dict[str, int]:
    """Return effective settings, defaulting the timeout."""
    return {
        "timeout_seconds": overrides.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    }
