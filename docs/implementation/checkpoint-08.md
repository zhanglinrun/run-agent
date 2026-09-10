# Checkpoint 08: Gateway runtime and durable steering

Date: 2026-09-10. This is an implementation checkpoint, not completion of the full redesign plan.

## Implemented

- `run gateway` assembles the persistent repository, bounded dispatcher, Coding applications,
  independent controller, explicit identity policy and Outbox. Waiting requests remain database
  rows; only actual runners own execution coroutines. Reservations survive slow cancellation
  and cleanup failure. The prior predecessor-chain scheduler is removed.
- `/status`, `/tasks`, `/stop`, `/cancel`, `/new` and `/steer` are connected. New-session
  replacement waits for actual foreground reservation release. Adapter ingress reserves
  control capacity and preserves earlier same-chat admissions across stop/new/steer barriers.
- `/steer` creates a bounded durable input task. Busy input records `target_run_id` and enters
  `steering`; idle input enters `queued`. Both retain source-message deduplication and arrival
  sequence. Pending steering reserves normal waiting capacity and its possible future deliveries.
- The Core accepts an optional asynchronous message source. Coding supplies a generic
  `InputBoundary` containing its writer token, head and pending final-looking reply. Gateway
  commits that reply, the next correction, its `consumed_entry_id`, and consumed Outbox in one
  SQLite transaction. Core and Coding do not import Gateway or interpret its task schema.
- `consumed` means durable admission to the target run's history. It is not a claim that a model
  request completed or followed the correction. The target run has a separate terminal result.
  Cancellation after the input commit preserves the input and does not run it a second time.
- If completion wins the consumption race, unconsumed steering becomes a normal queued task
  in the same completion transaction. Its task ID, sequence, session, epoch and original
  destination remain fixed. Outbox orders accepted, queued notification and final result.
- Stop/new cancel old-session pending inputs. Restart converts unconsumed inputs to queued
  while keeping an unreconciled workspace quarantined; consumed inputs are not requeued.
- Feishu uses the API v2 adapter contract, bounded ingress, stable per-chunk delivery UUIDs,
  and Outbox retries. The Gateway process lock excludes another local owner process.

## Evidence

- `gateway-runtime-tests.xml`: 136 passed, 1 skipped across `tests/redesign`.
  The skip is Windows symlink creation, not a Gateway test.
- `test_gateway_steering.py`: 9 tests exercise real Coding boundaries, duplicate messages,
  rollback, finish/conversion transactions, idle mode, waiting limits, stop, owner replacement,
  channel receipts, model failure and cancellation while an input commit settles.
- `test_gateway_runtime.py` and `test_gateway_repository.py`: FIFO, bounded live coroutines,
  cancellation, lease retention, atomic history/outcome/Outbox and delivery retry evidence.
- `test_feishu_adapter.py`: SDK mocks verify input identity and stable delivery UUIDs.
- `mypy`: 156 source files pass. Ruff passes for source, extensions, scripts and redesign tests.
- Wheel and sdist built offline into `.run/redesign/dist-08-steering`; installed into the fresh
  `.run/redesign/install-env-08-steering` environment.
- `distribution-check-08.json`: installed wheel import outside the source tree, only `run`
  registered, command routing, SQLite reopen, host services, frozen Skills, live Gateway and
  steering pass. It also runs the actual installed `run gateway` command twice against a local
  HTTP fixture and a test channel: two tasks, one resumed session, duplicate on restart ignored,
  all deliveries sent, no unreleased attempts, no JSONL output.

All model responses in this checkpoint are deterministic fixtures. No real provider or Feishu
credentials were used. These checks are reliability evidence, not model quality or throughput.

## Current Schema

Gateway namespace version is 3. Only new-database initialization and exact-version reopen are
supported. Versions 1 and 2 are development states and have no migration or fallback. Channel
extension API version remains 2; it is independent of the database namespace version.

## Still Required

- Independent background Session/worktree, fixed source history/resources, patch/report artifacts,
  original-channel completion, and `/new` isolation. `/background` remains explicitly unavailable.
- OS process groups / Windows Job Objects, lifecycle evidence, recovery reconciliation and an
  operator command to clear quarantine after verified cleanup.
- Mixed-load and crash experiments, account-level provider request budgets and retention limits.
- Complete resource-provider capture/restore and remaining snapshot equivalence requirements.
- Experience extension, explicit assets, review candidates, evaluation-bound promotion and rollback.
- Independent evaluation environments, held-out experiments and reproducible evidence manifests.

The full goal remains active. Requirements not covered by direct evidence stay open.
