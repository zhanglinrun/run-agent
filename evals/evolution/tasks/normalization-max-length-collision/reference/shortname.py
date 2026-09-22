import hashlib
import re


def shorten(value: str, max_length: int = 24) -> str:
    if max_length < 10:
        raise ValueError("max_length must be at least 10")
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(normalized) <= max_length:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    prefix = normalized[: max_length - 9].rstrip("-")
    return f"{prefix}-{digest}"
