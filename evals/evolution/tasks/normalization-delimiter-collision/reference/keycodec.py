def encode(parts: list[str]) -> str:
    return "".join(f"{len(part)}:{part}" for part in parts)


def decode(payload: str) -> list[str]:
    parts: list[str] = []
    position = 0
    while position < len(payload):
        colon = payload.find(":", position)
        if colon < 0:
            raise ValueError("missing length delimiter")
        length_text = payload[position:colon]
        if not length_text.isascii() or not length_text.isdigit():
            raise ValueError("invalid part length")
        length = int(length_text)
        start = colon + 1
        end = start + length
        if end > len(payload):
            raise ValueError("truncated part")
        parts.append(payload[start:end])
        position = end
    return parts
