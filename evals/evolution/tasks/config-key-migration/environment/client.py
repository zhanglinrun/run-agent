import settings


def endpoint(overrides: dict[str, object]) -> str:
    return f"{settings.resolve(overrides)['api_host']}/v1"
