import settings


def build(overrides: dict[str, int] | None = None) -> str:
    """Describe the client configured with resolved settings."""
    resolved = settings.resolve(overrides or {})
    return f"timeout={resolved['timeout_ms']}ms"
