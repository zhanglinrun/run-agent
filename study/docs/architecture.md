# Extension-first architecture and source evidence

This document maps architecture claims to the code that enforces them. It is an
evidence index, not a duplicate implementation guide.

## Dependency direction

```text
agents/runtime
  <- providers
  <- extensions
  <- harness
  <- app / cli / evaluation
```

`agents/runtime/core.py` imports provider contracts plus runtime contracts/hooks only.
It does not import Harness, Policy, MCP, Memory, Skills, Verification, Correction, or
Evaluation. `AgentCore` accepts a `ProviderAdapter`, `ToolExecutor`, tool definitions,
and `AgentHooks`.

`agents/harness/harness.py` owns task/session evidence and drives the extension
lifecycle. `agents/harness/core_session.py::CoreSession` binds one already-loaded
`ExtensionHost` to `AgentCore`; it does not assemble optional capabilities.

## Public extension kernel

| Claim | Enforcement source |
| --- | --- |
| Versioned `setup(api)` API | `agents/extensions/contracts.py::EXTENSION_API_VERSION`, `ExtensionAPI`; `agents/extensions/loader.py::load_extension_spec` |
| Tools, events, commands, prompts, services, execution factories | `agents/extensions/contracts.py::ExtensionAPI` |
| Dependency sorting and missing/cycle errors | `agents/extensions/host.py::ExtensionHost._sort_specs` |
| Atomic setup rollback | `agents/extensions/host.py::_snapshot`, `_restore`, `load` |
| Duplicate registration rejection and explicit replacement | `agents/extensions/host.py::register_tool`, `register_command`, `provide_service`, `register_execution_factory` |
| Project code requires explicit trust | `agents/harness/task.py::ExtensionSettings.trust_project`; `agents/extensions/loader.py::discover_extension_specs`; `agents/cli.py::--trust-project-extensions` |
| Deterministic default profile | `agents/extensions/defaults.py::_DEFAULTS`, `default_extension_specs` |
| Capability disablement is dependency-closed | `agents/extensions/defaults.py::default_extension_specs` |

Built-ins are ordinary `ExtensionSpec` values. External modules can replace a specific
registration only by passing `replace=True`; accidental duplicate names fail loading.

## Lifecycle

`agents/harness/harness.py::AgentHarness._run_bound` executes this order:

1. Create SQLite session, budget ledger, workspace journal, and trace.
2. Discover and load extension specs.
3. Ask the registered execution factory for the task execution environment.
4. Build the immutable base prompt.
5. Bind `ExtensionContext` and emit `session_start`.
6. Emit `before_run`, then run `AgentCore`.
7. Emit `after_solve`, then `after_run`.
8. Emit `session_shutdown`, close execution, and write `TaskResult`.

Commands registered by extensions use the same loaded task context but do not pretend
to be model solves or trigger verification.

## Prompt and context evidence

| Claim | Enforcement source |
| --- | --- |
| Immutable per-turn system prompt | `agents/extensions/host.py::transform_context` starts from `extension_context.base_prompt` each turn |
| Tool help comes from active registrations | `agents/extensions/host.py::transform_context` reads `ToolRegistration.prompt_snippet` and `prompt_guidelines` |
| Base prompt has no MCP/Memory/Skill assembly | `agents/context/prompt.py::build_system_prompt` |
| Workspace is task-scoped across concurrent SDK tasks | `agents/runtime/scope.py`; binding in `agents/harness/harness.py::run` |
| Structured episode/working/tool fold | `agents/context/folding.py`; `agents/extensions/context.py::setup_context` |
| Fold replaces the projected raw prefix on resume | `agents/session/reducer.py::SessionReducer.messages` |
| Retained recent messages survive compaction | `agents/extensions/context.py::compact_context` appends the compaction and canonical recent messages, then resets middleware projection |
| Duplicate message occurrences are preserved | `agents/harness/middleware.py::SessionTaskMiddleware` tracks ordered context projection rather than content-set membership |
| Semantic memory selection | `agents/context/memory.py::select_relevant_memories`; context hook in `agents/extensions/context.py::setup_memory` |
| Recalled memory is request-scoped | `setup_memory.recall` adds memory to the rebuilt system prompt, not session messages |
| Durable memory writes use a dedicated tool | `setup_memory.memory_save`; `agents/context/memory.py::save_memory` |

Memory recall has a 60 KiB task-request budget and excludes already surfaced paths
within that task. Recall uses a side model query over bounded metadata, then injects
only selected full entries.

## Tool and permission evidence

| Claim | Enforcement source |
| --- | --- |
| One schema validator before every registered tool | `agents/extensions/host.py::before_tool_call`; `agents/tools/schema.py` |
| Only active tools can execute | `agents/extensions/host.py::_tool_names_for`; `ExtensionToolExecutor.execute` |
| Deferred activation is context-local and ceiling-bound | `ExtensionHost.search_tools`, `ExtensionContext.tool_ceiling_names`; one host is created per task |
| One authorization service for model tools and MCP | `agents/extensions/policy.py::setup_permissions`, `_authorize`; final enforcement in `ExtensionHost.before_tool_call`; `ExtensionContext.authorize` |
| File workspace boundary | `agents/policy/engine.py::_path_decision`; `agents/execution/local.py::_resolve`; Docker workspace mount |
| Unknown extension tools do not auto-allow | final branch of `agents/policy/engine.py::PolicyEngine.decide` |
| Host Shell is explicit trust | `agents/harness/task.py::ExecutionSettings.allow_host_shell`; `agents/extensions/workspace.py::session_start` |
| MCP startup is authorized before process creation | `agents/extensions/policy.py::setup_mcp.session_start` |

Local subprocess `cwd` is not represented as a filesystem sandbox. Therefore the
built-in profile removes `run_shell` for local execution unless `allow_host_shell`
is explicitly enabled. Docker is the isolated Shell path.

## Plan Mode evidence

- Persisted state and workspace-local plan path:
  `agents/extensions/policy.py::setup_plan`, `_plan_path`.
- Missing or rejected approval cannot exit:
  `setup_plan.exit_plan`.
- Plan is evaluated before bypass/project allow rules:
  `agents/policy/engine.py::PolicyEngine.decide`.
- Entering/restoring Plan Mode removes `run_shell` from the active parent ceiling:
  `setup_plan.enter`, `setup_plan.session_start`.
- The only write exception compares paths through `WorkspaceBoundary`:
  `PolicyEngine.decide`.
- Child permission state copies the parent's current mode, and children cannot exit an inherited Plan Mode:
  `agents/extensions/subagents.py::SubagentService.run`;
  `agents/extensions/policy.py::setup_plan.exit_plan`.

The `agent` meta-tool may run in Plan Mode, but the child receives the same `plan`
mode, the parent's Shell-free active tools, and cannot recursively create another
child. A Coder child therefore cannot elevate the parent ceiling.

## Skills and sub-agent evidence

Effective child tools are computed in `SubagentService.run`:

```text
host current active tools
  intersection role.allowed_tools
  intersection Skill allowed_tools (when present)
  minus recursive agent
  minus Shell for read-only roles
```

Both model-visible child schemas and `ExtensionToolExecutor.allowed_names` use that
same set. The child receives a copied `PermissionState` whose mode is inherited and
whose `read_only` bit can only become stricter.

`agents/evolution/skills.py::_load_skill_file` normalizes malformed `allowed-tools`
to an empty list, not unrestricted access. `agents/extensions/skills.py::skill`
applies the same hard intersection to inline Skills or passes it to a forked child.

Online Skill evolution is implemented by `setup_skill_evolution` lifecycle handlers.
Plan runs do not consume or replace pending evidence. Candidate transitions are
checked in `agents/evolution/candidates.py::promote_candidate`; only
`pending_evaluation` can move to rejected, activation_failed, or promoted.

## Verification and correction evidence

| Claim | Enforcement source |
| --- | --- |
| Verification runs before correction | dependency/order in `agents/extensions/defaults.py`; handlers in `agents/extensions/quality.py` |
| Solve and repair budgets are separate | `agents/harness/budget.py::BudgetLedger` |
| Usage and cost are charged each turn | `agents/runtime/hooks.py::TurnResult.usage`; `agents/harness/middleware.py::BudgetMiddleware` |
| Side queries and children share the ledger | `ExtensionContext.side_query` charges a phase turn plus usage; child uses the parent `BudgetLedger` in `SubagentService.run` |
| Failed acceptance affects final status | `agents/extensions/quality.py::setup_acceptance` |
| Patch candidates carry SHA and report association | `quality.py::save_candidate`; `TaskState.candidate_reports` |
| Restore is temporary-workspace-only and hash checked | `quality.py::restore_candidate` |
| Restore checks base index before destructive steps | `restore_candidate` uses temporary `GIT_INDEX_FILE`, `read-tree`, and `git apply --check --cached` |
| Failed restore does not select the candidate | final candidate selection in `setup_correction` |

Coding and SWE-bench tasks reject an initially dirty workspace in
`AgentHarness._run_bound`. Interactive dirty workspaces remain usable, but patch
export is withheld because a Git diff cannot reliably attribute pre-existing hunks
to the current task; changed-path evidence still compares current hashes with the
journal's initial snapshot.

## Removed duplicate paths

The `0.4` rewrite removes these production concepts:

- `FeatureFlags` and runtime services in `TaskSpec.metadata`.
- Legacy class-based `HarnessExtension` / `HarnessRunContext`.
- Legacy `HarnessSession` capability assembly.
- Global Tool Gateway, global deferred-tool activation, and duplicate local
  filesystem/Shell executors.
- Facade-owned MCP lifecycle and background Skill-evolution tasks.

`agents/tools/registry.py` now contains schemas only. Runtime execution is registered
by `agents/extensions/workspace.py`, and every tool goes through `ExtensionHost`.
