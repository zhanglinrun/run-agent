"""Small Windows compatibility hook for the Linux SWE-bench evaluator."""

from __future__ import annotations

from pathlib import Path


_write_text = Path.write_text


def _write_text_lf(self: Path, data: str, *args, **kwargs):
    if self.name == "eval.sh" and isinstance(data, str):
        encoding = kwargs.get("encoding") or (args[0] if args else None) or "utf-8"
        errors = kwargs.get("errors") or (args[1] if len(args) > 1 else None) or "strict"
        return self.write_bytes(data.replace("\r\n", "\n").replace("\r", "\n").encode(encoding, errors))
    return _write_text(self, data, *args, **kwargs)


Path.write_text = _write_text_lf
