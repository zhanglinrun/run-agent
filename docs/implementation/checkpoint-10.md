# Checkpoint 10: Managed command process lifetime

Date: 2026-09-10. This checkpoint does not complete the redesign.

## Implemented

- Built-in bash and terminal commands share a Session-owned ProcessSupervisor.
  Windows creates a hidden suspended command process, assigns it to a named Job Object
  with KILL_ON_JOB_CLOSE, then resumes its thread. Descendants inherit job membership.
  Job membership is verified empty before releasing native handles and ownership.
- POSIX starts each command in its own session/process group, sends TERM, escalates to
  KILL after a grace period, and verifies the group has disappeared. Root exit also
  drains surviving descendants; shell background commands do not outlive a tool call.
- Command output uses a temporary file rather than inherited pipes. Reads are capped
  at 32 MiB and a polling limit terminates excess output. The polling limit is not a
  hard filesystem quota; writes can exceed it between checks.
- Repeated coroutine cancellation and cancellation during spawn wait for ownership and
  cleanup. Supervisor shutdown cancels running work and rejects new commands.
- Unverified cleanup remains owned. Coding refuses a final result with unresolved
  commands, and Gateway retains the assignment slot and quarantines its workspace.
- Successful cleanup records the native identity, run/session/owner, termination actions
  and exit code through a durable SQLite observation append. These observations are
  diagnostics, not the authority for restarting or releasing an orphaned assignment.

## Evidence

- `process-tests.xml`: 165 passed, 2 skipped across the Windows redesign suite. Skips are
  the POSIX TERM test and unavailable Windows symlink creation.
- `process-linux-tests.xml`: 12 passed, 1 Windows-only skip under WSL Ubuntu / Python 3.12.
- `test_process_supervisor.py` launches real commands and children: timeout, repeated
  cancellation, spawn cancellation, shutdown, root-exit cleanup, output limits, Windows
  host-process death, Linux TERM/KILL escalation, and Gateway cancellation/quarantine.
- The quarantine test injects a failed membership probe after real process termination;
  it verifies no terminal success, no released slot and no successor using that workspace.
- Full mypy: 159 source files pass. Ruff passes source, extensions, scripts and redesign tests.
- Fresh wheel/sdist in `.run/redesign/dist-10`; clean installation in
  `.run/redesign/install-env-10`. `distribution-check-10.json` includes a real installed
  terminal command plus launcher, SQLite, resources and three Gateway command launches.

Fixture model responses only; no real provider or Feishu credentials were used.

## Limits and Remaining Work

- New-process registration and recovery still need a durable intent/identity journal.
  Windows host death between native process creation and job assignment could leave a
  suspended process; it cannot execute user code but needs orphan reconciliation.
- POSIX process groups are not a security sandbox. A command deliberately creating a new
  session can escape group membership; restricted evaluation needs OS isolation.
- Third-party extensions spawning processes outside the host service are not covered.
- Gateway startup recovery and the operator quarantine-release command remain open.
- Provider/task budgets, retention, resource-provider snapshots, experience and independent
  evaluation/learning experiments remain part of the active full goal.

No database migration or legacy command/data compatibility was added.
