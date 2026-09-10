from defaults import DEFAULT_TIMEOUT_MS


def resolve(overrides: dict[str, int]) -> dict[str, int]:
    """Return effective settings, defaulting the timeout."""
    return {"timeout_ms": overrides.get("timeout_ms", DEFAULT_TIMEOUT_MS)}
