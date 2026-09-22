import settings


def endpoint(overrides: dict[str, object]) -> str:
    return f"{settings.resolve(overrides)['base_url']}/v1"
