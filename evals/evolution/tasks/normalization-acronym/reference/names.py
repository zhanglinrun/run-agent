import re


def to_snake(value: str) -> str:
    separated = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return re.sub(r"[\s-]+", "_", separated).lower()
