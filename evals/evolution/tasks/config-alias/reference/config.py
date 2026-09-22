DEFAULTS: dict[str, object] = {"color": "red", "size": "m"}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    provided = dict(overrides)
    alias = provided.pop("colour", None)
    if "color" not in provided and alias is not None:
        provided["color"] = alias
    return {**DEFAULTS, **provided}
