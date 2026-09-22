import keyword
import re


def to_identifier(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_").lower()
    if not result:
        return "_"
    if result[0].isdigit():
        result = "_" + result
    if keyword.iskeyword(result):
        result += "_"
    return result
