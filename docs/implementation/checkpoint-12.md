# Checkpoint 12: Gateway recovery and explicit workspace review

Date: 2026-09-10. The full redesign remains active.

## Implemented

- Windows binds a suspended process to its Job Object atomically with CreateProcessW
  JOB_LIST attributes. Linux executes a small startup gate whose stdin must receive the
  host's authorization after native identity is persisted. Parent death before that
  authorization cannot execute the user command. Recovery checks pre-record gates too.
- Gateway host records include machine identity, PID and native creation identity. With
  the correct exclusive process lock, a replacement can establish the old host has exited
  without waiting for a stale lease timeout. A live or foreign-machine host is rejected.
- Startup inspects retained assignments before channel startup. Committed terminal results
  retain their content and delivery ID; verified dead owners/processes permit resource
  release without a duplicate result. Interrupted work remains outcome_unknown.
- `run gateway recover` inspects without loading channels or model configuration.
  `--terminate RUN_ID` terminates verified orphan process ownership; Linux uses pidfds
  for member signals. `--release RUN_ID --note TEXT` requires dead processes and a present
  workspace, then atomically records the review, releases the reservation and publishes
  the original-session unknown-outcome result. It never changes unknown effects to success.
- Recovery rechecks process records and workspace/run ownership at commit. Failure rolls
  back slot release, workspace release, process reconciliation and Outbox together.
- Sessions marked for recovery cannot be reopened by another CLI writer, even with a
  takeover request. A local review clears that restriction after native checks.
- Restored backup packages cannot start Gateway or resend old deliveries before explicit
  restore reconciliation. The complete relocation/reconciliation workflow remains open.

## Evidence

- `recovery-tests.xml`: Windows full suite, 177 passed and 2 platform skips.
- `recovery-linux-tests.xml`: 23 passed, 2 Windows-only skips in WSL Ubuntu.
- `test_gateway_recovery.py`: 8 tests include real host death before prompt, after atomic
  completion, during shell execution and between spawn/native registration; live-owner
  refusal, no CLI bypass, transactional rollback, command-line review and restore guard.
- Linux tests leave a real orphan shell group, reject premature release, terminate its
  members and then release the workspace. Windows verifies Job Object disappearance.
- Mypy passes 164 source files; Ruff passes. A fresh wheel/sdist and clean installation
  at `.run/redesign/dist-12-final` / `.run/redesign/install-env-12-final` pass launcher,
  native terminal command, Gateway restart/background and recovery inspection checks.
- `distribution-check-12.json` records the installed command validation. No real model
  or channel credentials were used; model responses and channels are local fixtures.

## Schema and Remaining Work

Shared SQLite schema is 8; Gateway namespace schema is 5. Fresh initialization and exact
version reopen only, with no migrations, legacy aliases or JSONL fallback.

Recovery manages supported host-owned commands. Arbitrary trusted extension subprocesses
outside the host service and deliberate POSIX session escape are not OS-isolated. Backup
relocation, extension/handler compatibility, resource-provider versions, budgets, retention,
experience and independent evaluation still require implementation. P3-7 remains in progress.
