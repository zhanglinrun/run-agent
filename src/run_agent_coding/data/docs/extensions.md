# Run Agent extensions

Run Agent has two explicit Python extension boundaries:

- Agent extensions add capabilities to one `CodingSession`.
- Gateway extensions add channel adapters to the multi-session Gateway host.

Both are trusted executable Python, not configuration data or a sandbox.

## Agent extensions

An Agent extension synchronously exports `setup(api)`:

```python
from run_agent_coding.extensions import ExtensionAPI
from run_agent_core.tools import AgentTool, AgentToolResult


async def execute(call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content=f"hello {arguments['name']}")


def setup(api: ExtensionAPI) -> None:
    api.register_tool(
        AgentTool(
            name="hello",
            label="Hello",
            description="Return a greeting.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            execute_fn=execute,
        )
    )
```

The API can register tools, event hooks, slash commands, prompt guidelines or
sections, dynamic providers, custom message renderers, and UI components. `setup`
must only register capabilities; asynchronous work and cleanup belong in tools or
lifecycle handlers.

Every regular extension receives:

- `context.environment`: an immutable process-environment snapshot, including the
  effective `.env` values loaded by the host.
- `context.paths`: canonical user storage roots for sessions, traces, and
  extension-owned state.
- session metadata such as cwd, model, provider, session id, thinking level, and
  a read-only copy of the active transcript.

## Discovery and installation

Agent extensions load from:

- `~/.run/extensions/` by default;
- an explicit `run-agent -e PATH` argument;
- `<project>/.run/extensions/` only after project approval and the explicit
  `--project-extensions` opt-in.

Install a trusted local file, directory, or pinned Git source with:

```bash
run-agent install ./path/to/extension.py
run-agent install ./path/to/extension-directory
run-agent install git:github.com/owner/repository@v1.2.0
```

The installer copies code into `~/.run/extensions/`. It does not install Python
dependencies or provide update/remove package management.

Setup failures are isolated and roll back all registrations from that source.
Reload creates a fresh runtime generation; using a captured API, context, or UI
handle from the retired generation raises a stale-generation error.

## Tool execution metadata

`AgentTool.execution_mode` is `parallel` or `sequential`. The default read tool is
parallel-capable; write, edit, and bash are sequential. A batch containing a
sequential tool runs entirely in order. A pure parallel batch is bounded by the
session's `max_parallel_tools`, converges cancellation, and emits final results in
the provider's declaration order.

Extensions can use `tool_call` and `tool_result` hooks for policy or observation.
A hook failure is contained by the host; tool-call hook failures block the call
instead of silently bypassing policy.

Tool policy runs in the Agent loop, not in `AgentTool.execute()`. Calling a tool
executor directly only executes that tool. Embedders using `AgentHarness` without
CodingSession must install `runtime.before_tool_call` and
`runtime.after_tool_call` in its configuration. `compose_tools()` merges definitions
and does not install policy. Core `before_tool_call` callbacks return
`BeforeToolCallResult` (or `None`), replacing the former `(blocked, reason)` tuple.

`ToolCallHookEvent` contains `tool_call_id`, `tool_name`, and prepared `arguments`.
Return `ToolCallHookResult(block=True, reason=...)` to publish an error without
executing the tool, or return `arguments` to replace its inputs. A blocked result
with `terminate=True` contributes to the loop's all-tools-terminate stop rule.
`ToolResultHookEvent` includes the same call identity and actual execution
arguments, plus `result` and `is_error`. It runs after successful and failed tool
executions, before result persistence. `ToolResultHookResult` can override content,
details, error status, or termination. Preparation failures and blocked calls skip
this hook. Observer events still report their error results.

## Prompt and context hooks

`before_agent_start` runs after input and Skill/template expansion for a new user
prompt. Return `BeforeAgentStartResult` with an optional `system_prompt` and a tuple
of `CustomMessage` objects. Overrides chain in registration order and apply only
to the current run. Custom messages are persisted through the normal message
events. Queued steering/follow-up messages stay in that run; they do not restart
this hook. Cancellation and errors release preparation state and clear overrides.

`context` runs before each model request in the Agent loop. Its `ContextEvent`
contains a detached `messages` tuple. Return `ContextHookResult(messages=...)` to
change that request without rewriting the transcript. Each handler sees the
previous successful transform. A handler that raises cannot leak mutations to
the saved transcript or the next handler's input. This is suitable for transient
retrieval context or request pruning. Compaction and session naming use separate
CodingSession-owned requests and do not dispatch this hook.

```python
from run_agent_coding.extensions import BeforeAgentStartResult, ContextHookResult
from run_agent_core import CustomMessage


def setup(api):
    @api.on("before_agent_start")
    def prepare(event, context):
        return BeforeAgentStartResult(
            system_prompt=event.system_prompt + "\nReview changes before finishing.",
        )

    @api.on("context")
    def contextualize(event, context):
        return ContextHookResult(
            messages=(*event.messages, CustomMessage(
                custom_type="task-context",
                content="Prefer the repository's existing test commands.",
                display=False,
            )),
        )
```

Tools and prompt contributions registered by an active extension become visible
before the next model turn. Resources from disk change through explicit reload;
they are not re-read every turn. Setup and generation invalidation remain the
same for these hooks as for other extension registrations.

## Official optional extensions

The repository's top-level `extensions/` directory contains:

- `mem0`: remote Mem0-backed durable `memory` tool;
- `mcp`: MCP Streamable HTTP bridge tool;
- `observability`: per-session JSONL span recorder and `/trace`;
- `permission_policy`: mutating-tool policy hook;
- `plan_mode`: read-only `/plan` mode;
- `verification`: structured deterministic `verify` tool.

None is loaded automatically or included as a core wheel package. Load one with
`-e extensions/<name>` or install it with
`run-agent install extensions/<name>`.

Mem0 requires `MEM0_API_KEY` and never falls back to a local JSON memory store.
MCP reads the `RUN_AGENT_MCP_SERVERS` JSON object. Permission policy reads
`RUN_AGENT_PERMISSION_MODE=allow|guarded|ask|deny`. Missing extensions contribute
no corresponding tools or policy.

## Gateway extensions

A Gateway channel module synchronously exports `setup_gateway(api)`:

```python
from collections.abc import AsyncIterator

from run_agent_gateway import InboundMessage, OutboundMessage

GATEWAY_EXTENSION_API_VERSION = 1
GATEWAY_EXTENSION_NAME = "my-channel"


class Adapter:
    name = "my-channel"

    async def messages(self) -> AsyncIterator[InboundMessage]: ...

    async def send(self, message: OutboundMessage) -> None: ...

    async def close(self) -> None: ...


def setup_gateway(api) -> None:
    api.register_adapter(Adapter())
```

Adapter names must be unique. API-version mismatch, invalid methods, duplicate
registration, import failure, or setup failure atomically rolls back that module.
The adapter owns channel credentials and SDK resources; the Gateway host owns
session mapping, scheduling, model selection, and `CodingSession` lifecycle.

Use `examples/extensions/` for Agent examples and
`examples/gateway_extensions/` for Feishu and stdin JSONL Gateway adapters.
