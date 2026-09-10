# Checkpoint 09: Independent background workspaces

Date: 2026-09-10. The full redesign remains in progress.

## Implemented

- `/background <content>` captures a clean Git commit, clones only the active source
  ancestry into a new Session, preserves principal/project identity and records the
  source head, watermark, resource entry, route epoch and original notification target.
- A detached worktree beneath the state directory contains the pinned commit. Relative
  working directories are preserved. Dirty/non-Git input, submodules, linked files and
  unverified existing worktrees are explicitly rejected. This is not an OS sandbox.
- First background use initializes source resources through the Coding application
  without a model prompt. Background execution restores pinned Skill packages and
  system overrides, and rejects changed extension prompt contributions or trust policy.
- Background outcomes archive a binary patch, untracked files and a content-addressed
  manifest before the terminal Session/task/Outbox transaction. Capture rechecks HEAD,
  tracked diff, status and untracked content. Commits created inside the detached
  worktree are supported if the source commit remains an ancestor. No automatic merge.
- Results and cancellation preserve output artifacts. `/new` leaves the background
  Session and original destination intact and does not copy output into new history.
- Two bounded preparation workers leave controls responsive. A route reservation keeps
  ordinary runners from opening the source during resource initialization. Stop/new
  fence delayed admission, and shutdown drains preparation before releasing ownership.
- A separate drained monitor releases reservations even when preparation is cancelled
  before its first instruction. Persisted duplicates bypass preparation capacity and
  source revalidation, but still reject reused message IDs with different content.

## Evidence

- `background-tests.xml`: 153 passed, 1 Windows symlink skip across the full redesign suite.
- `test_background_workspaces.py`: 17 tests cover real Git worktrees, real write-tool
  execution with fixture model responses, branch ancestry, fixed resources, original
  notification identity, dirty-input rejection, rollback, cancellation and preparation races.
- Mypy passes for 157 source files; Ruff passes for source, extensions, scripts and tests.
- Wheel/sdist built offline in `.run/redesign/dist-09-final` and installed into the fresh
  `.run/redesign/install-env-09-final` environment.
- `distribution-check-09.json` validates the installed launcher outside the source tree.
  The actual `run gateway` command is launched three times against a local HTTP fixture:
  foreground execution, restart/deduplication/resume, then first-use background execution
  in another chat. Three tasks, three sessions, all deliveries sent, with worktree artifacts.

No real model or Feishu credentials were used. These are correctness checks, not model
quality measurements or production performance claims.

## Schema and Limits

Gateway namespace version is 4, with exact-version reopen and fresh initialization only.
There is no migration, JSONL fallback, old command alias or old extension API adapter.
Result collection limits are 1024 untracked files and 32 MiB; retained worktree disk usage
and process output quotas still need their own lifecycle policy.

## Remaining Work

- OS process groups / Windows Job Objects, verified exit, cancellation escalation and
  startup/operator reconciliation. Unknown executions remain quarantined.
- Complete resource-provider and extension/tool version capture and restore. The current
  implementation pins Skills and detects prompt changes, not arbitrary extension code.
- Relocated backup execution recovery: artifact backup integrity is verified, but Git
  repository/worktree absolute paths still need explicit relocation and reconciliation.
- Provider/root-task budgets, retention, mixed-load/crash experiments, experience extension,
  independent evaluation environments and held-out learning evidence.

G10 is complete with direct tests; P3-4 remains in progress pending lifecycle/recovery work.
