import unicodedata


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()
