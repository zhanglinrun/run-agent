"""Child-side startup barrier; EOF means the host died before authorizing execution."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if os.read(0, 1) != b"G":
        raise SystemExit(125)
    with open(os.devnull, "rb") as stream:
        os.dup2(stream.fileno(), 0)
    _identity, shell, command = sys.argv[1:]
    os.execlp(shell, shell, "-c", command)


if __name__ == "__main__":
    main()
