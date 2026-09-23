# Architecture

Run Agent has three runtime layers: Provider, Core and Coding. Observability and Evals are supporting modules. Do not introduce higher-level host imports into Core or make Coding depend on a concrete evaluator implementation.

| Layer | Responsibility |
| --- | --- |
| `run_agent_ai` | OpenAI-compatible and Anthropic transports, retries, streaming and usage |
| `run_agent_core` | Provider-neutral messages, AgentHarness/loop, tool transactions and generic session contracts |
| `run_agent_coding` | CodingSession, CLI/TUI, resources, extension host, Context Budget guard and JSONL session tree |

`run_agent_entry.py` routes `run` and `run bench`. `CodingApplication` owns startup/shutdown and every interactive, print or benchmark session uses the same CodingSession lifecycle.

## Durable session model

Core does not import Coding paths or UI. `SessionStorage.compare_and_append(entries, expected_head)` is the atomic write seam: the JSONL adapter takes one cross-process lock, reads the active leaf, checks the expected head and parents, assigns sequence numbers, then commits. Single rows use tail append plus `fsync`; batches use a same-directory temporary file, `fsync`, `os.replace` and directory `fsync`.

A session is a parent-linked tree plus append-only `LeafEntry` pointers. `/rewind` appends a pointer and preserves abandoned branches. `/fork` copies the selected root path, including resource activation, compaction and associated `RunCommitEntry` records. A run commit fixes its start/end, status, snapshot and error; `agent_settled` is emitted only after that fact is durable. `HistoryService.read_completed_run(run_id)` reads exactly the committed interval, not the later session transcript.

Workspace `index.jsonl` and the user catalog are append-only last-write-wins metadata logs with lock-protected compaction. Project indexes are authoritative; the catalog is a rebuildable discovery cache. Old `state.sqlite3` files are not sessions.

## Provider context

The durable transcript is not the Provider request. The loop first applies extension context transforms and provider-safe tool-history repair. Coding then runs three steps over the frozen view:

1. the extension's `before_provider_request` rewrite (compaction lives here, not in the core);
2. `ContextBudgetGuard.freeze` measures the rewritten request (`tokens_before`/`tokens_after` and the stable prefix digest) without rewriting anything;
3. a request still above the model window is refused with `ContextBudgetExceeded` before physical Provider I/O.

Compaction is owned by the `compaction` built-in (`run_agent_extensions/layered_compaction`), which loads by default, decides and rewrites the request in `before_provider_request` (a gate at 80% of the budget, then L1 persist / L2 mid-view snip / L3 old-result placeholder, then an L4 summary only while the view is still over the gate), and requests the durable commit over `session_compact_request` (validated by the core, which still writes the single `CompactionEntry`). Without that extension - for instance under `--no-extensions` - there is no compaction at all and only the hard window guard remains.

The memory layer is extension-owned and prompt-only. The `memory` built-in (`run_agent_extensions/hermes_memory`) renders `USER.md` / `MEMORY.md` into a frozen `before_agent_start` section and a request-local `<memory-context>` fence from the `context` hook, and it never rewrites a durable message or the session JSONL. The core has no memory store of its own.

The final detached request and its measurements are frozen in the model-input snapshot before physical Provider I/O. The core writes no context artifacts: nothing derived is spilled to disk, so the JSONL history is the only transcript.

## Extension lifecycle

Session extensions export synchronous `setup(api)`. Every registration belongs to a source and generation. Failed setup removes that source's tools, commands, hooks, providers, status and task handlers.

Reload and session replacement use a staged runtime. The old generation remains active until successor host publication commits. It then enters a read-only retiring state, receives shutdown notification, clears source-owned UI, retires registrations and drains explicit disposers in reverse order. MCP connection closure is a disposer responsibility. Direct Python file/network effects remain outside this lifecycle and cannot be rolled back; extensions are trusted code, not sandboxed code.

## Experience and evaluation

Experience is an extension, and so is memory. The `memory` built-in keeps `USER.md` and `MEMORY.md` as bounded Markdown stores and owns their prompt injection; the `experience` built-in owns Skill evolution. Formal Skill content is immutable to ordinary model tools: `skill_manage propose` creates a loader-invisible candidate bound to a committed run, base digest and bounded operations. Project claims require trusted read-only probes.

`EvaluationService` is a host contract. Local CLI sessions expose an unavailable implementation, which keeps candidates cold. The eval host may inject a paired evaluator; reports bind request, baseline and measured candidate hashes. Publication rechecks report, probes, ownership, pin and base digest under the Skill root lock before atomically replacing one SKILL.md and recording the ledger.

Extension tasks are in memory and do not survive a process crash. Candidate and session logs are durable. Observations append under the configured state root. Backups contain session/index files only; memory files, Skills and candidate assets are ordinary user/project files and follow their own ownership policy.

## Remaining runtime behavior

AgentHarness owns transcript state, steering/follow-up queues, listeners and cancellation. Tool batches run in parallel only when every call declares parallel execution; mixing any sequential tool serializes the batch and results are returned in source order. This is a correctness policy, not an original scheduling algorithm.

Settings merge `~/.run/settings.json` with trusted project settings. `shellCommandPrefix` and `defaultProjectTrust` are user-only. Projects may set queue modes; provider, model and thinking remain environment-based. A legacy `compaction` key is ignored, because the `compaction` extension owns compression.
