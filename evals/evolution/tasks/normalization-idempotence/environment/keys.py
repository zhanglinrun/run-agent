import re


def normalize_key(value: str) -> str:
    body = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return f"key-{body}"
