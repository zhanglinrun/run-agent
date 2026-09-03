"""JSON parsing helpers shared by Skill extraction and evaluation."""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(raw[start : end + 1])
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}
