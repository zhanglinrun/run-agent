import re


def shorten(value: str, max_length: int = 24) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:max_length].rstrip("-")
