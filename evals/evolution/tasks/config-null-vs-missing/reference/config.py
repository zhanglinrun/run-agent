DEFAULTS: dict[str, object] = {"label": "worker", "timeout": 30}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return {
        "label": overrides["label"] if "label" in overrides else DEFAULTS["label"],
        "timeout": overrides["timeout"] if "timeout" in overrides else DEFAULTS["timeout"],
    }
