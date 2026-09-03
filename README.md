# Run Agent

Run Agent is a provider-neutral Python agent core with a Pi-style extension host.
`AgentCore` owns only the model/tool turn loop. Everything else is composed from
Python extensions: execution, typed tools, permissions, Plan Mode, MCP, context
folding, memory, Skills, sub-agents, verification, correction, and acceptance.

The `0.4` API is intentionally breaking. Runtime services no longer travel through
`TaskSpec.metadata`, and there is no `FeatureFlags`, global Tool Gateway, or
class-based Harness extension stack.

## Architecture

```text
AgentCore
  - ProviderAdapter
  - ModelContext + turn loop
  - ToolExecutor protocol
  - AgentHooks + EventBus

AgentHarness
  - TaskSpec / RuntimeConfig / BudgetLedger
  - SQLite session and trace evidence
  - ExtensionHost lifecycle
  - TaskResult aggregation

ExtensionHost
  - setup(api) discovery and dependency ordering
  - tools / events / commands / prompt contributions
  - execution factory and shared services
  - per-task active-tool state
```

Default lifecycle:

```text
load extensions
  -> create execution environment
  -> bind task context
  -> session_start
  -> before_run
  -> AgentCore solve
  -> after_solve (verification, then correction)
  -> after_run (Skill state, acceptance)
  -> session_shutdown
```

The built-in profile lives in `agents/extensions/defaults.py`. Each capability is
independently disableable; disabling a required extension also removes its built-in
dependents.

## Built-in Extensions

| Extension | Responsibility |
| --- | --- |
| `execution` | Local or Docker execution factory |
| `workspace-tools` | Typed file tools, deferred-tool search, optional Shell |
| `permissions` | One authorization service for model tools, MCP, and children |
| `plan` | Persisted read-only Plan Mode and approval gate |
| `context` | Structured episode/working/tool context folding |
| `memory` | Semantic recall and dedicated durable-memory writes |
| `subagents` | Bounded Coder/Reviewer/Verifier lanes |
| `skills` | Workspace-aware discovery and hard `allowed-tools` ceilings |
| `skill-evolution` | Candidate-first online Skill evolution |
| `mcp` | Authorized MCP startup, dynamic tools, and shutdown |
| `verification` | Environment-grounded completion report |
| `correction` | Bounded repair and hash-checked candidate recovery |
| `acceptance` | Final acceptance commands that affect task status |

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Configure either an OpenAI-compatible or Anthropic-compatible provider in `.env`:

```text
APIKEY=...
API=https://provider.example/v1
MODEL=provider-model
```

## CLI

```powershell
# REPL
python -m agents

# One task
python -m agents "Inspect the repository and explain its architecture"

# Persisted Plan Mode
python -m agents --plan "Design the requested refactor"

# Docker-backed tool execution
python -m agents --sandbox docker "Implement the change"

# Explicitly trust host-local Shell access
python -m agents --allow-host-shell "Run a local diagnostic"

# Disable capabilities
python -m agents --disable-extension mcp --disable-extension skill-evolution

# Load a trusted extension path
python -m agents -e examples/extensions/audit.py

# Execute project-local Python extensions only with explicit trust
python -m agents --trust-project-extensions
```

Host-local `run_shell` is not model-visible by default because setting a subprocess
working directory is not a filesystem sandbox. `--allow-host-shell` is an explicit
trust decision. Docker mode provides the isolated command path. Reviewer and
Verifier children never receive Shell, and Plan Mode removes Shell from the parent
and inherited child ceiling.

## Python SDK

```python
from pathlib import Path

from agents import Agent

agent = Agent(
    model="gpt-5",
    api_key="...",
    use_openai=True,
    workspace=Path.cwd(),
    disable_extensions=("mcp",),
)
result = await agent.run_once("Implement the requested change")
print(result.status, result.patch)
await agent.close()
```

For direct Harness integration, construct typed runtime settings:

```python
from agents import AgentHarness, RuntimeConfig, TaskSpec
from agents.harness import ProviderSettings

runtime = RuntimeConfig(
    provider=ProviderSettings(
        model="gpt-5",
        api_key="...",
        use_openai=True,
    )
)
result = await AgentHarness().run(
    TaskSpec("task-1", "Inspect this project", Path.cwd(), mode="interactive", runtime=runtime)
)
```

## Third-party Extensions

A trusted Python module exports synchronous `setup(api)`. Handlers may be sync or
async. Tool handlers return a normal value for success or `ToolHandlerResult` for an
explicit success/failure result. The versioned API supports:

- `api.register_tool(...)`
- `api.on(event, handler)`
- `api.register_command(...)`
- `api.contribute_prompt(...)`
- `api.provide(...)`
- `api.register_execution_factory(...)`
- active/deferred tool controls

```python
EXTENSION_API_VERSION = 1
EXTENSION_NAME = "example-audit"
EXTENSION_REQUIRES = ("workspace-tools",)


def setup(api):
    async def command(args, context):
        return f"session={context.state.session_id} args={args}"

    api.register_command("audit", command, description="Show task audit identity")
```

Discovery order is built-ins, user extensions, trusted project extensions, then
explicit paths. User extensions live in `~/.run/extensions`. Project extensions
live in `.run/extensions` and are executable only when `trust_project=True` or
`--trust-project-extensions` is supplied. Explicit paths are trusted by the caller.

See [extension API](study/docs/extensions.md) and the
[example extension](examples/extensions/audit.py).

## Evidence and Safety

- SQLite sessions are append-only and preserve lanes, operations, custom extension
  state, and structured compactions.
- System prompts are rebuilt from immutable base state every model turn; Memory and
  Skills contributions do not append to the prior generated system prompt.
- Child tools are `parent active tools ∩ role tools ∩ Skill allowed-tools`.
- Missing Plan approval fails closed. Only the workspace-local plan artifact may be
  written while Plan Mode is active.
- File tools enforce workspace containment at policy and execution boundaries.
- Unknown extension tools require confirmation in interactive modes and are denied
  in `dontAsk`.
- Coding/SWE-bench modes reject dirty workspaces so generated patches cannot absorb
  pre-existing user changes.
- Candidate restoration is restricted to disposable temporary workspaces and checks
  artifact ownership, SHA-256, base commit, and `git apply --check` before mutation.

Direct source evidence for each claim is maintained in
[architecture evidence](study/docs/architecture.md).

## Evaluation

```powershell
run-agent-coding-benchmark evals/coding/smoke/tasks.jsonl `
  --model <model> --max-cost 0.50

run-agent-swebench campaign --limit 2 --model <model> --max-cost 2.00 --grade

python -m agents.evaluation.benchmarks gaia --problem-type text --model <model>
```

Coding and SWE-bench default to Docker. `--sandbox local` does not expose model Shell
unless `--allow-host-shell` is also supplied. Agent and child model turns consume the
turn, token, and cost budgets. Side queries consume the shared token and cost budgets
and emit the same model request/response Trace events.
