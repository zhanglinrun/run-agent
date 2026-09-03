# Run Agent architecture

Run Agent `0.4` uses a Pi-style extension host around a minimal provider-neutral
`AgentCore`.

```text
AgentCore = ProviderAdapter + turn loop + ToolExecutor + AgentHooks

AgentHarness = TaskSpec + RuntimeConfig + Budget + SQLite + Trace + TaskResult

Default profile = execution + workspace tools + permissions + Plan + MCP
                + structured context + memory + Skills + sub-agents
                + verification + correction + acceptance
```

Capabilities are registered through the public Python `setup(api)` contract in
`agents/extensions/contracts.py`. Built-ins and trusted third-party modules use the
same tool, event, command, prompt, service, and execution-factory APIs. The CLI loads
the complete built-in profile by default and can disable each named capability.

The source-level architecture and claim-to-code evidence table is in
[`docs/architecture.md`](./docs/architecture.md). Third-party extension authoring is
in [`docs/extensions.md`](./docs/extensions.md).

Key boundaries:

- `agents/runtime/` has no imports from Harness, Policy, Context, MCP, Skills,
  Verification, Correction, or Evaluation.
- `agents/harness/` owns task/session evidence and extension lifecycle only.
- `agents/extensions/` owns all optional capability assembly.
- `agents/tools/` owns JSON schemas and validation, not execution or global state.
- `agents/execution/` owns local/Docker implementations and workspace observation.
- `agents/session/` is the append-only authority for resume, lanes, operations, and
  structured compaction.
- `agents/evaluation/` constructs typed `RuntimeConfig` and consumes `TaskResult`.

Security boundaries are explicit: project Python extensions require trust; host-local
Shell is absent unless explicitly enabled; Plan Mode fails closed without approval
and removes Shell; child tools are the intersection of parent, role, and Skill
ceilings; unknown extension tools require confirmation or fail closed in `dontAsk`.
