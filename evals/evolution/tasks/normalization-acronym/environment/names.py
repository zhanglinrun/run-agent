import re


def to_snake(value: str) -> str:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[\s-]+", "_", separated).lower()
