def encode(parts: list[str]) -> str:
    return "|".join(parts)


def decode(payload: str) -> list[str]:
    return payload.split("|")
