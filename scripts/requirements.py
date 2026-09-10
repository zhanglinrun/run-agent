"""Read, update and check the canonical requirements ledger.

Evidence entries are either a repository-relative path, or ``path::symbol`` when
the claim is backed by a specific test function. ``check`` verifies both the path
and, when given, that the symbol actually appears in that file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "implementation" / "requirements.json"
STATUSES = ("pending", "in_progress", "complete")


def load(path: Path = LEDGER) -> dict:
    """Parse the ledger."""
    return json.loads(path.read_text(encoding="utf-8"))


def save(ledger: dict, path: Path = LEDGER) -> None:
    """Write the ledger in a deterministic, diff-stable form."""
    body = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")


def entries(ledger: dict) -> list[dict]:
    """Every tracked task and acceptance entry."""
    return [*ledger["tasks"], *ledger["acceptance"]]


def find(ledger: dict, ident: str) -> dict:
    """Return one entry by id."""
    for entry in entries(ledger):
        if entry["id"] == ident:
            return entry
    raise SystemExit(f"unknown id: {ident}")


def check_evidence(entry: dict, root: Path) -> list[str]:
    """Report missing evidence paths and missing named symbols."""
    problems = []
    for item in entry["evidence"]:
        path, _, symbol = item.partition("::")
        target = root / path
        if not target.exists():
            problems.append(f"{entry['id']}: missing evidence path {path}")
        elif symbol and symbol not in target.read_text(encoding="utf-8"):
            problems.append(f"{entry['id']}: {symbol} not found in {path}")
    return problems


def check(ledger: dict, root: Path = ROOT) -> list[str]:
    """Report every ledger inconsistency."""
    problems = []
    for entry in entries(ledger):
        problems.extend(check_evidence(entry, root))
        if entry["status"] == "complete" and not entry["evidence"]:
            problems.append(f"{entry['id']}: complete without evidence")
        if entry["status"] not in STATUSES:
            problems.append(f"{entry['id']}: unknown status {entry['status']}")
    return problems


def counts(ledger: dict) -> dict[str, dict[str, int]]:
    """Status tallies for tasks and acceptance separately."""
    result: dict[str, dict[str, int]] = {}
    for section in ("tasks", "acceptance"):
        tally: dict[str, int] = {}
        for entry in ledger[section]:
            tally[entry["status"]] = tally.get(entry["status"], 0) + 1
        result[section] = tally
    return result


def cmd_set(args: argparse.Namespace, ledger: dict) -> int:
    """Set status and replace or append evidence."""
    entry = find(ledger, args.id)
    entry["status"] = args.status
    if args.evidence is not None:
        entry["evidence"] = list(args.evidence)
    entry["evidence"].extend(args.add_evidence)
    save(ledger)
    print(f"{args.id}: {args.status} evidence={entry['evidence']}")
    return 0


def main() -> int:
    """Dispatch set / check / counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    setter = actions.add_parser("set")
    setter.add_argument("id")
    setter.add_argument("status", choices=STATUSES)
    setter.add_argument("--evidence", nargs="*")
    setter.add_argument("--add-evidence", nargs="*", default=[])
    actions.add_parser("check")
    actions.add_parser("counts")
    args = parser.parse_args()
    ledger = load()
    if args.action == "set":
        return cmd_set(args, ledger)
    if args.action == "counts":
        print(json.dumps(counts(ledger), indent=2))
        return 0
    problems = check(ledger)
    for problem in problems:
        print(problem)
    print(f"ledger check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
