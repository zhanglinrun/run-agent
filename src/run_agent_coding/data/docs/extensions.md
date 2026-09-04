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
