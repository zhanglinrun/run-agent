import json


def dumps(config: dict[str, object]) -> str:
    return json.dumps({key: str(value) for key, value in config.items()}, sort_keys=True)


def loads(payload: str) -> dict[str, object]:
    return json.loads(payload)
