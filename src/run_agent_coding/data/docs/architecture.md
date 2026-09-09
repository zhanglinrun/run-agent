# Architecture

Run Agent has four layers: Provider, Core, Coding and Gateway. Observability and Evals are supporting modules. Do not add a fifth runtime package or introduce Gateway imports into Coding's application, storage or host contracts.

| Layer | Responsibility |
| --- | --- |
| `run_agent_ai` | Provider transports, request retries, streaming and usage |
| `run_agent_core` | Messages, model/tool loop, cancellation, generic session contracts |
| `run_agent_coding` | CodingSession, unified terminal, application lifecycle, resources, extensions and SQLite |
| `run_agent_gateway` | Channel ownership, routing and multi-session execution |

`run_agent_entry.py` routes `run`, `run gateway` and `run bench` without eagerly loading other hosts. `CodingApplication` owns startup and shutdown and executes parsed command intents asynchronously. Interactive and print modes differ in presentation. The Gateway session pool shares the application lifecycle and owns one SessionManager/database across its sessions.

Core cannot import Coding's database or UI. Its `SessionStorage` contract exposes paged reads, branch heads, expected-head writes, run qualifications, forks and completion receipts. SQLite implementations live in Coding. No file-format compatibility reader exists for former sessions.

A SQLite session writer has a session/owner/run/generation token and an ownership lease. `begin_run` creates a durable execution attempt. Intermediate events commit during the loop; the final answer and following context changes are staged for `complete_run`. That transaction appends the final entries, advances the branch head, records the execution outcome and invalidates its run token. Only then may `agent_settled` be emitted. A composable outcome committer allows the Gateway to add its task outcome and Outbox to this same transaction; that Gateway integration is still in progress.

Short ownership transactions must resolve their result before releasing in-memory handles, including when cancelled. Final cleanup closes the model generator and reconciles message persistence. Unfinished attempts found when acquiring an abandoned session become `outcome_unknown`; they are not automatically replayed. Qualification checks cannot undo external tool side effects.

Branches preserve prior history. Forking and adding a branch summary happen in one transaction. SQLite provides the active branch head; no leaf-pointer event or full-file rewrite is needed. Cold open loads paged events; an active session caches entries. Context snapshot optimization and bounded long-history replay remain separate implementation tasks.

Session extensions export `setup(api)`. Gateway adapters export `setup_gateway(api)` and belong to the Gateway host. MCP, plan, permission and verification remain optional Session behavior. UI extensions use notifications, confirmation/input/selection and source-owned status text; they cannot mount framework widgets. Reload retires old API generations and clears their status. This is lifecycle management, not an OS sandbox.

The default coding tools are `read`, `write`, `edit` and `bash`. The existing tool batching behavior remains: bounded pure-read concurrency and serial execution for batches containing mutation-capable calls. Improvements are evaluated against real tasks and failure cases, not presented as an original scheduling algorithm.

Consult `docs/implementation/requirements.json` for remaining managed tasks, experience, durable Gateway and independent evaluation work. Planned capabilities are not implied by these interfaces alone.
