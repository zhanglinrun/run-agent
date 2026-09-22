import re


def normalize_key(value: str) -> str:
    body = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    while body.startswith("key-"):
        body = body[4:]
    if body == "key" or not body:
        return "key"
    return f"key-{body}"
