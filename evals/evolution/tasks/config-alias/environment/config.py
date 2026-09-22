DEFAULTS: dict[str, object] = {"color": "red", "size": "m"}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return {**DEFAULTS, **overrides}
