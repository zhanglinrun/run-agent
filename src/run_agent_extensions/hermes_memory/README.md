# Hermes Memory

A self-contained memory extension ported from hermes-agent. It ships the provider
contract, the fan-out manager, the built-in `MEMORY.md` / `USER.md` file provider with
hermes' frozen-snapshot semantics, and the `setup(api)` wiring that registers the
`memory` tool and the `/memory` command.

This package is the built-in `memory` extension
(`run_agent_extensions.BUILTIN_EXTENSIONS`): new sessions load it by default, and it can
also be loaded explicitly:

```text
run --extension memory
run --extension src/run_agent_extensions/hermes_memory
```

It is the only registrar of the `memory` tool and the `/memory` command; the
`experience` extension owns Skill evolution only. Loading both extensions is safe and
expected — they no longer share a name.

## Files

| File | Responsibility |
| --- | --- |
| `provider.py` | `MemoryProvider` ABC (hermes' full contract), `RecallStatus`, the trivial-prompt gate, tool-schema normalization, context fencing and the streaming scrubber. |
| `manager.py` | `MemoryManager`: registration limits, per-provider failure isolation, recall fan-out plus indicator, the single-worker commit queue, session-boundary ordering and the bounded shutdown drain. |
| `store.py` | Built-in file provider: `MemoryFile` (budget, lock, drift guard, batch), `MemoryStore` (live entries + frozen snapshot), `BuiltinMemoryProvider`, `run_memory_call`, and the thin wrappers over the project's shared write guards. |
| `extension.py` | `setup(api)`: config, scope resolution, hook wiring, the `memory` tool, `/memory`, and the request-local recall injection helper. |
| `__init__.py` | Stable re-exports only. |

## Read and write paths

| Target | File | Default scope | Default budget |
| --- | --- | --- | --- |
| `memory` | `MEMORY.md` | project: `<cwd>/.run/MEMORY.md` | 2200 characters |
| `user` | `USER.md` | user: `<home>/USER.md` (`<home>` = `RunAgentPaths.home`, `~/.run` by default) | 1375 characters |

The `memory` tool takes `target` (`memory` or `user`), `action`
(`add`/`replace`/`remove`/`batch`), `content`/`old_text`/`new_content`/`new_text`,
`operations[]`, and an optional `scope` (`project` or `user`) that overrides the
target's default. `/memory show|add|replace|remove <user|memory> ... [--scope project|user]`
is the hand-driven path and reports the same messages the tool returns.

When the project is untrusted (`context.project_resources_enabled` is False, i.e. the
project trust policy declined or is unanswered) the project scope contributes no
snapshot section and accepts no write: a project-scoped call is refused with
`project inputs are untrusted in this session`.

## Lifecycle

| Hook | What this extension does |
| --- | --- |
| `session_start` (startup/reload/new/resume/branch) | Loads both scopes, builds the built-in provider and the manager, and **freezes** the snapshot blocks. Every session change re-fires this hook, so it is also the snapshot refresh point. |
| `before_agent_start` | Appends the cached `# Long-term memory` section to this run's system prompt. The string is cached for the session, so the prompt prefix is byte-stable and no disk read happens per turn. |
| `input` | Records the turn's prompt, resets the per-turn consolidation budget, and runs the `is_trivial_prompt`-gated `prefetch_all`. The recall indicator (`MemoryManager.describe_recall`) is notified to the UI when one is attached. |
| `context` | Injects the fenced recall block as ONE request-local user message (see below). |
| `turn_start` | `MemoryManager.on_turn_start(turn_index, prompt)` per turn. |
| `agent_settled` | `sync_all(user, assistant, transcript)` and `queue_prefetch_all` for the next turn. |
| `session_before_compact` | Returns `SessionBeforeCompactResult(context=on_pre_compress(transcript))`: the compaction extension fences that text into its summary prompt as material, and nothing is remembered for the next request. Fired by the `compaction` extension right before it commits a compaction, so a session with no compaction extension (or one whose compaction is cancelled) never fires it. |
| `session_before_switch` (`/new`, `/resume`) | `commit_session_boundary_async` + bounded `flush_pending`. |
| `session_shutdown` | `commit_session_boundary_async` (end-of-session extraction) + `flush_pending` + `shutdown_all`. |

Frozen snapshot: the prompt block is captured at `session_start` and never moves while
the session runs. A write lands on disk immediately and is visible to tool responses,
but the next session (or `/reload`) is the first time the model sees it. That is what
keeps the provider prefix cache valid, and it is hermes' own rule, not an adaptation.

Memory and session history are separate layers, exactly as in hermes: the files above
are never compacted and never become a summary (the compaction package does not read
them at all), and the only thing a compaction may take from memory is the provider text
returned by `on_pre_compress` through the `session_before_compact` gate.

### Recall injection (difference from hermes)

hermes appends the fenced block to the **API copy** of the current turn's user message
and persists that copy as an `api_content` sidecar so the next turn replays the same
bytes. Run Agent's `context` hook hands extensions a detached message snapshot, so this
package appends a **separate** request-local user message instead:

- durable session history is never rewritten — not even the current turn's user message;
- the same bytes are replayed for every request of the turn (the block is only rebuilt
  on the next `input`);
- the block is not persisted anywhere, so a reopened session shows the clean transcript.

An empty recall renders nothing at all.

## Failure semantics

- **One external provider.** `add_provider` always accepts `name == "builtin"`; a second
  non-builtin provider is rejected with a warning and ignored. Provider tools whose name
  collides with the reserved surface (`read`, `write`, `edit`, `bash`, `grep`, `find`,
  `ls`, `memory`) or with an already routed provider tool are dropped individually;
  built-ins always win, exactly as in hermes.
- **Isolation.** Every fan-out (`prefetch_all`, `sync_all`, `queue_prefetch_all`,
  `build_system_prompt`, `initialize_all`, `on_*`) catches per-provider exceptions and
  logs them. A failing provider never breaks another provider and never reaches the
  caller.
- **Ordered writes.** All provider writes run on one background worker, so turn N lands
  before turn N+1. `commit_session_boundary_async` submits `on_session_end` and
  `on_session_switch` as one FIFO task, which makes "extraction strictly before
  rebinding" a structural property; `on_session_switch` still fires when extraction
  raises, and a provider that raises is isolated. The worker is created lazily, so the
  builtin-only path spawns no thread.
- **Bounded, explicit drain.** `flush_pending(timeout)` waits on a barrier and, on
  timeout, cancels the queued tasks and reports them as abandoned
  (`status="timed_out"`, `abandoned_writes`, `abandoned_prefetches`, `active_tasks`)
  instead of dropping them silently. Repeated calls with no new submission in between
  return the recorded outcome (idempotent). `shutdown_all(timeout)` gives the futures
  queued at shutdown one bounded window, then reports the leftovers and shuts providers
  down in reverse order. Submissions after shutdown are rejected with a warning.
- **File-memory refusals.** A write is refused, and the file left untouched, when: the
  budget would be exceeded (the current entries are echoed back so the model can
  consolidate), `old_text` matches no entry or several distinct entries, the content
  would not round-trip through the `§`-delimited format, the file on disk contains
  content the tool did not write (backed up to `<file>.bak.<unix-ts>` first), or the
  file exists but cannot be read. Three at-capacity failures in one turn produce a
  terminal "stop retrying" answer so a fragile consolidation cannot loop the turn.
- **Threats.** Writes are scanned with the project's shared pattern library at the
  `strict` scope (`run_agent_extensions.experience.threats`, imported, never copied).
  At snapshot-build time each entry is scanned again and a match is replaced **in the
  snapshot** with `[BLOCKED: ...]`; the live entry and the on-disk file keep the original
  text so a user can see and delete exactly what was blocked.
- **Gates.** Every mutation goes through the shared writeback/revocation gate
  (`experience.mutation.require_mutation`, which honours
  `run_agent_coding.host.learning`'s writeback switch) and, when configured, the
  approval hook (`experience.write_approval.approve_write`). The imports are absolute
  because this package is loaded as its own extension package; the wrappers in
  `store.py` only pin the scope and arguments, they never re-implement a rule.

## Configuration (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_MEMORY_CHAR_LIMIT` | `2200` | `MEMORY.md` budget (>= 100). |
| `HERMES_MEMORY_USER_CHAR_LIMIT` | `1375` | `USER.md` budget (>= 100). |
| `HERMES_MEMORY_ENABLED` | `true` | Expose the `memory` target at all. |
| `HERMES_MEMORY_USER_PROFILE_ENABLED` | `true` | Expose the `user` target at all. |
| `HERMES_MEMORY_WRITE_APPROVAL` | `false` | Require an interactive confirmation before a write. |
| `HERMES_MEMORY_PREFETCH_TIMEOUT` | `8.0` | Per-provider prefetch timeout for non-builtin providers. |
| `HERMES_MEMORY_DRAIN_TIMEOUT` | `5.0` | Bounded drain window for `flush_pending`/`shutdown_all`. |

## Differences from hermes-agent

Ported 1:1 (documented behaviour, not just signatures): the `MemoryProvider` contract
including every optional hook's meaning; the trivial-prompt regex and gate; the
`<memory-context>` fence, system note, `sanitize_context` stripping and
`StreamingContextScrubber` state machine; `normalize_tool_schema`; the
one-external-provider and reserved-tool-name rules; per-provider failure isolation;
prefetch fan-out with an external-provider thread and "skip while still running"; the
recall indicator text (`recalled N memories` / `recalled 1 memory` /
`recalled relevant memory`); the single-worker FIFO queue; end-before-switch boundary
ordering; the frozen snapshot and `[BLOCKED: ...]` snapshot masking; entry delimiter,
budgets, drift backup, sidecar lock, atomic write, batch-all-or-nothing, ambiguity and
round-trip refusals, and the per-turn consolidation budget.

Equivalent simplifications and adaptations:

| hermes | here | Why |
| --- | --- | --- |
| `messages: list[dict]` (OpenAI shape) | `Sequence[AgentMessage]` | Run Agent's canonical message types. |
| `_strip_skill_scaffolding` before prefetch/sync | not ported | Run Agent expands prompt templates in the session layer; extension hooks already see the user's own text. |
| `memory_provider_tools_enabled(..., resolve_toolset)` | literal `"memory"` name check | Run Agent has no toolset registry. |
| `_HERMES_CORE_TOOLS` | `RESERVED_TOOL_NAMES` | The built-in coding tools plus `memory`. |
| `_submit_background` copies contextvars for profile isolation | same wrap | Here it also matters for `host.learning`'s writeback ContextVar: a worker thread would otherwise start with writeback enabled. |
| `flush_pending(timeout) -> bool` | `-> FlushResult` | The caller must be able to report abandoned writes, not just "timed out". |
| `notify_memory_tool_write(...)` | `_mirror_write` inside the extension | The decision (committed? mutating? batch expansion? provenance) is the same; it lives with the tool that owns the result. |
| `on_memory_write` signature introspection (keyword/positional/legacy) | always keyword `metadata` | The ABC here defines the keyword parameter, so every subclass accepts it. |
| `save_config(values, hermes_home)`, `get_config_schema`, `backup_paths` | present on the ABC, unused by this extension | Nothing in this distribution consumes provider config schemas or the external backup walk; the contract is kept so a ported provider still type-checks. |
| `initialize_all` injects `hermes_home` | the caller passes `home=str(paths.home)` | Run Agent resolves paths from `RunAgentPaths`, not a process-global. |
| `commit_session_boundary_async(..., reason=...)` forwards `reason` to providers | logged, not forwarded | No provider in this distribution needs it; the ABC still accepts extra kwargs. |
| hermes has no equivalent | `inject_recall_block` (request-local message) | See the injection section above. |

Hooks hermes calls that Run Agent has no place for:

| hermes | Status here |
| --- | --- |
| `on_turn_start(turn, message, **kwargs)` | Wired to the `turn_start` observation event; the event carries `turn_index` only, so the message text comes from the last `input` hook. |
| `on_session_switch(new_id, reset=..., rewound=...)` | Wired to `session_start` (refresh/rebind) and `session_before_switch` (end before switch). `rewound` is never produced: Run Agent has no `/undo` path that fires this extension. On `/new` the new id does not exist yet when the switch hook runs, so the new identity arrives via the next `session_start` → `initialize(session_id)`. |
| `on_session_end(messages)` | Wired: `session_before_switch` and `session_shutdown` both run it through the ordered boundary task. |
| `on_pre_compress(messages) -> str` | Called on `session_before_compact`, which the `compaction` extension emits just before its own commit. The returned text goes back through the gate's result (`SessionBeforeCompactResult(context=...)`) and the compaction extension fences it into the summarizer prompt as material, labelled as reference data rather than an instruction. Nothing is carried into the next request's memory block: memory and session history stay separate layers. |
| `on_delegation(task, result, ...)` | No caller: this distribution has no subagent delegation surface. The manager hook and the provider default exist so a ported provider still works. |
| `handle_tool_call` routing for provider tools | Implemented and reachable through `MemoryManager.handle_tool_call`; this extension registers no provider tools of its own (`BuiltinMemoryProvider.get_tool_schemas()` is empty by design — file memory is the built-in `memory` tool). |
| `on_memory_write` mirroring to external providers | Implemented (`MemoryManager.on_memory_write`, skipping the builtin writer) and called with provenance (`write_origin`, `execution_context`, `session_id`, `tool_name`, `old_text`) after a committed write. With no external provider registered it is a no-op, which is the current state of this package. |
| `backup_paths()` | Declared on the ABC; this distribution's backup tooling does not walk provider paths, so it is never called. |

## Tests

`tests/redesign/test_hermes_memory_*.py`, all offline:

- `test_hermes_memory_provider.py` — trivial-prompt boundaries (including `k8s`, `yolo`,
  `note`), fence formatting/stripping, streaming scrubber across chunk boundaries,
  schema normalization (both shapes and nameless rejection), tool enablement.
- `test_hermes_memory_manager.py` — one-external-provider limit, reserved tool names,
  per-provider isolation on every fan-out, indicator wording for 0/1/N, "last prefetch
  only", FIFO ordering and `on_session_end` → `on_session_switch` (including an
  extraction that raises), abandoned-write/prefetch reporting, idempotent flush and
  shutdown, post-shutdown rejection, JSON tool routing.
- `test_hermes_memory_store.py` — frozen snapshot (write is durable, prompt does not
  move, `load_from_disk()` moves it), `[BLOCKED: ...]` in the snapshot with the raw
  entry retained, budget refusal with the current entries, duplicate add, ambiguity,
  non-round-trip content, drift backup, unreadable file, batch all-or-nothing, usage
  string, provider ABC surface, `run_memory_call` refusals.
- `test_hermes_memory_extension.py` — `setup()` registration surface, the pinned call
  surface, config parsing, scope resolution, byte-stable
  paths, command happy/refusal paths, and one end-to-end run through
  `CodingApplication` that proves every subscribed hook name exists, the `/memory`
  command is dispatchable, and the snapshot stays frozen for the session.
