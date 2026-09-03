import re


def slugify(value: str) -> str:
    normalized = value.strip().lower()
    return re.sub(r"\s", "-", normalized)
