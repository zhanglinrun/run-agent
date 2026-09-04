"""Detect and normalize files dragged into the terminal.

Terminals do not deliver OS drag-and-drop as a dedicated event. When a file is
dropped onto the terminal window, the terminal emulator types the file's path
into the running program instead. Because Textual enables bracketed-paste mode,
that typed path usually arrives as a single :class:`textual.events.Paste`
message.

The exact text depends on the terminal:

- most terminals shell-escape paths (``/tmp/my\\ file.png``) and separate
  multiple dropped files with spaces;
- some quote paths with spaces (``"/tmp/my file.png"``);
- some VTE-based terminals emit ``file://`` URIs;
- a few emit the bare path, even when it contains spaces.

This module recognizes pasted text that consists solely of one or more existing
absolute paths and normalizes it to clean, space-separated filesystem paths,
quoting any path that contains whitespace.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

__all__ = ["normalize_dropped_paths"]


def normalize_dropped_paths(text: str) -> str | None:
    """Return normalized prompt text when *text* looks like a file drop.

    The pasted text is treated as a drop only when it consists exclusively of
    one or more absolute paths that exist on disk (shell-escaped, quoted, or
    ``file://`` URI forms are accepted). Anything else returns ``None`` so the
    paste falls through to default handling.
    """
    stripped = text.strip()
    if not stripped:
        return None

    # A single dropped file may arrive as a bare path with unescaped spaces.
    whole = _token_to_path(stripped)
    if whole is not None:
        return _quote_path(whole)

    tokens = _split_drop_tokens(stripped)
    if not tokens:
        return None

    paths: list[str] = []
    for token in tokens:
        path = _token_to_path(token)
        if path is None:
            return None
        paths.append(path)
    return " ".join(_quote_path(path) for path in paths)


def _split_drop_tokens(text: str) -> list[str] | None:
    """Split shell-style dropped paths without treating Windows separators as escapes."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "\\" and index + 1 < len(text) and text[index + 1].isspace():
            current.append(text[index + 1])
            index += 2
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quote is not None:
        return None
    if current:
        tokens.append("".join(current))
    return tokens


def _token_to_path(token: str) -> str | None:
    """Resolve one dropped token to an existing absolute path, if possible."""
    candidate = token
    if candidate.startswith("file://"):
        raw_path = unquote(candidate.removeprefix("file://"))
        if re.match(r"^[A-Za-z]:[\\/]", raw_path):
            # Windows terminals commonly emit the non-standard file://C:\\... form.
            candidate = raw_path
        else:
            parsed = urlparse(candidate)
            if parsed.netloc not in ("", "localhost"):
                return None
            candidate = url2pathname(unquote(parsed.path))
            if re.match(r"^/[A-Za-z]:[\\/]", candidate):
                candidate = candidate[1:]
    path = Path(candidate)
    if not path.is_absolute() or not path.exists():
        return None
    return candidate


def _quote_path(path: str) -> str:
    """Quote *path* with double quotes when it contains whitespace."""
    if not any(char.isspace() for char in path):
        return path
    escaped = path.replace('"', '\\"')
    return f'"{escaped}"'
