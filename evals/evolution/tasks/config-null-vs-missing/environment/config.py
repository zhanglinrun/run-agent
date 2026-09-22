DEFAULTS: dict[str, object] = {"label": "worker", "timeout": 30}


def resolve(overrides: dict[str, object]) -> dict[str, object]:
    return {
        "label": overrides.get("label") or DEFAULTS["label"],
        "timeout": overrides.get("timeout") or DEFAULTS["timeout"],
    }
