# Checkpoint 11: Durable native process identities

Date: 2026-09-10. The full goal remains active.

## Implemented

- `managed_processes` records launch intent before spawning: process ID, Session/run/owner
  generation, host PID and native creation identity, workspace and command hash. Command
  bodies are not copied into the process journal.
- Windows records Job Object name, command PID and creation identity before resuming the
  suspended thread. A failed durable registration cleans up the job without executing the
  user command. POSIX records process-group and creation identity after spawn; its launch
  interval remains a conservative recovery uncertainty.
- Native identity uses Windows process creation FILETIME or Linux boot ID/start ticks.
  A reused PID cannot be treated as the same process based solely on its number.
- Exit recording requires verified empty membership and matching run ownership. An old
  process may report its own cleanup after revocation but cannot launch new work. SQLite's
  Session completion transaction refuses unresolved launching/running process records.
- Observations remain a secondary copy. Recovery must inspect the required journal and
  the operating system; it cannot infer release authority from a missing diagnostic span.
- SQLite schema is now version 7. The initializer writes schema/application IDs from its
  constants in the same transaction as schema creation, removing duplicate SQL literals.
  Only fresh initialization and exact-version reopen are supported.

## Verification

- `process-journal-tests.xml`: 169 passed, 2 platform skips across the Windows suite.
- `process-journal-linux-tests.xml`: 14 passed, 3 Windows-only skips on WSL Ubuntu.
- A real Coding host is terminated while its shell child runs. The Windows job drains,
  but SQLite keeps the unfinished execution and native identities for later reconciliation.
- Registration failure before Windows resume creates no child output. Journal ownership,
  unverified exit rejection, completion blocking and stale launch authority are tested.
- Full mypy passes 161 source files; Ruff passes. Fresh wheel/sdist and installed launcher
  checks pass in `.run/redesign/dist-11-final` and `.run/redesign/install-env-11-final`.
- `distribution-check-11.json` confirms a real installed terminal command and Gateway
  startup/restart/deduplication/cold-background flows using local fixture model responses.

## Remaining

The journal is a recovery prerequisite, not a completed recovery implementation. Startup
reconciliation, operator inspection/release, spawn-gap resolution and relocated worktrees
remain open. A vanished process does not establish whether external side effects succeeded.
Resource-provider snapshots, budgets, retention, experience extension, independent evaluation
and held-out experiments also remain open. No real model or Feishu credentials were used.
