import re

_DEVICES = {"CON", "PRN", "AUX", "NUL"}
_DEVICES.update(f"COM{number}" for number in range(1, 10))
_DEVICES.update(f"LPT{number}" for number in range(1, 10))
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_segment(value: str) -> str:
    result = _INVALID.sub("_", value).strip(" .")
    if not result or result in {".", ".."}:
        return "_"
    stem = result.split(".", 1)[0].upper()
    if stem in _DEVICES:
        result = "_" + result
    return result
