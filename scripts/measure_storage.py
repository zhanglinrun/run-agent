"""Measure SQLite session storage and write the result as a JSON artifact.

Run: ``python scripts/measure_storage.py [--sizes 1000,10000] [--output PATH]``

The measurement logic lives in ``scripts/measurelib/storage.py``; this file is
only the command line and the report line per size.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from measurelib.storage import DEFAULT_SIZES, measure_all  # noqa: E402

DEFAULT_OUTPUT = ROOT / ".run" / "verify" / "storage-measurements.json"


def report_line(sample: dict) -> str:
    """One summary line per measured history size."""
    lag = sample["write_load_loop_lag"]
    return (
        f"{sample['entries']:>7} entries  populate {sample['populate_ms']:8.1f}ms  "
        f"append {min(sample['append_ms']):.2f}-{max(sample['append_ms']):.2f}ms  "
        f"read {sample['paginated_read_ms']:8.1f}ms  fork {sample['fork_ms']:6.2f}ms  "
        f"lag p95 {(lag['p95_ms'] or 0):.1f}ms  lag samples {lag['samples']}"
    )


def main() -> int:
    """Measure the requested sizes and write the artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizes", default=",".join(str(size) for size in DEFAULT_SIZES))
    args = parser.parse_args()
    sizes = tuple(int(part) for part in args.sizes.split(",") if part.strip())
    report = measure_all(sizes, ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    for sample in report["samples"]:
        print(report_line(sample))
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
