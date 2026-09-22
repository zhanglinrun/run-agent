import json


def dumps(config: dict[str, object]) -> str:
    return json.dumps(
        config,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def loads(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return value
