# Python extension API

Run Agent extension modules are executable Python. User and explicit-path extensions
are loaded according to configuration; project `.run/extensions` code requires
explicit trust.

## Module contract

```python
from agents.extensions import EXTENSION_API_VERSION, ToolHandlerResult

EXTENSION_NAME = "my-extension"
EXTENSION_REQUIRES = ("workspace-tools",)


def setup(api):
    assert api.api_version == EXTENSION_API_VERSION
```

`setup(api)` must be synchronous so registration is deterministic and atomic. Tool,
event, command, prompt, and execution handlers may return a value directly or an
awaitable. A setup exception rolls back every registration made by that extension.

Optional module constants:

- `EXTENSION_API_VERSION`: must equal the runtime version when declared.
- `EXTENSION_NAME`: dependency graph name; defaults to filename/package name.
- `EXTENSION_REQUIRES`: iterable of extension names loaded first.

## Registration API

### Tools

```python
def setup(api):
    async def handler(value, context):
        return {"session_id": context.state.session_id, "value": value["value"]}

    api.register_tool(
        {
            "name": "example_echo",
            "description": "Return a value with session identity.",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
        handler,
        prompt_snippet="`example_echo` returns a tagged value.",
        prompt_guidelines=("Use it only when a tagged echo is requested.",),
        deferred=True,
    )
```

The host validates input against the registered schema, lets extension event handlers
narrow or normalize it, then runs the shared authorizer as the final gate and
validates any authorized replacement again. Tool name and call id are immutable.
Only active tools can execute. A deferred tool is activated within the current
context and its hard `tool_ceiling_names`. Unknown custom tools require user
confirmation under the default policy.

Use `ToolHandlerResult(content, ok=False, error="error_code")` for an expected tool
failure. Plain strings, mappings, and other handler values are successful results;
the host does not infer failure from output text. Unexpected failures should raise an
exception, which the host records as a failed tool result.

Use `replace=True` only for intentional replacement of an existing registration.
Accidental duplicates fail extension loading.

### Events

```python
def setup(api):
    async def before_run(event, context):
        context.append_state("my-extension", {"phase": "started"})

    api.on("before_run", before_run)
```

Built-in lifecycle events:

- `session_start`
- `before_run`
- `after_solve`
- `after_run`
- `session_shutdown`
- `context`
- `tool_call`
- `tool_result`
- `should_stop`
- `prepare_next_turn`

Handlers run in extension dependency/load order. `tool_result` hook composition runs
in reverse order through `AgentHooks`. Lifecycle state belongs in
`ExtensionContext.services` or append-only custom session entries, not module globals.

### Prompt contributions

```python
def setup(api):
    def render(turn, context):
        return f"Current workspace: {turn.workspace}"

    api.contribute_prompt("workspace-note", render, priority=50)
```

Every model turn starts from `ExtensionContext.base_prompt` and recomputes active tool
help plus prompt contributions. Do not mutate the prior generated system prompt.

### Commands

```python
def setup(api):
    async def status(args, context):
        return f"session={context.state.session_id}; args={args}"

    api.register_command("example-status", status, description="Show example status")
```

CLI/API command prompts use `/example-status optional arguments`. Commands run with a
bound task/session context and skip solve/verification lifecycle events.

### Services

```python
def setup(api):
    api.provide("my_service", object())
```

Consumers call `context.require("my_service")`. Services are copied into every child
context. A service must use the context argument or `context.host.current_context`
when behavior is task/child-specific; do not capture process-wide workspace state.

### Execution factory

```python
def setup(api):
    async def create(task, workspace):
        return MyExecutionEnvironment(workspace)

    api.register_execution_factory(create)
```

A custom profile must contain exactly one effective execution factory. The environment
implements the methods used by its registered tools, verification, patch export, and
cleanup. Use `replace=True` only when intentionally replacing another factory.

## Context object

`ExtensionContext` exposes:

- `task`, `state`, `workspace`
- `repository`, `journal`, `trace`
- `provider`, `execution`, `artifact_root`
- immutable `base_prompt`
- task services and active-tool ceiling
- `append_state(...)` / `latest_state(...)`
- `authorize(...)` for non-model actions such as external process startup
- `side_query(...)`, charged to the shared task token/cost budget and Trace
- mutable `outcome` for ordered completion extensions

Do not write external state or start processes during module import. Use
`session_start` and `session_shutdown`, and send non-model operations through the
shared authorizer.

## Discovery and trust

```python
from agents.harness import ExtensionSettings, RuntimeConfig

settings = ExtensionSettings(
    explicit_paths=(Path("/trusted/my_extension.py"),),
    load_user=True,
    trust_project=False,
    disabled=frozenset({"mcp"}),
)
runtime = RuntimeConfig(extensions=settings)
```

Locations:

- User: `~/.run/extensions/*.py` or package directories.
- Project: `<workspace>/.run/extensions`, disabled unless explicitly trusted.
- Explicit: file or directory paths supplied by the caller.

Directories discover top-level non-private `*.py` files and child packages containing
`__init__.py`. Modules are registered in `sys.modules` before execution so package
relative imports and normal Python decorators work.

## Default profile dependencies

```text
execution
  -> workspace-tools
       -> permissions
            -> plan
            -> memory
            -> subagents -> skills -> skill-evolution
            -> mcp
  -> verification -> correction
  -> acceptance
context
```

Disabling a built-in removes built-in dependents. External extensions with missing
requirements fail loading rather than running partially.

See [example extension](../../examples/extensions/audit.py) and
[source evidence](architecture.md).
