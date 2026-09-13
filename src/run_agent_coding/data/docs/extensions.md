# Session extensions

A Session extension is a Python module exporting synchronous `setup(api)`. Explicit files load with `run --extension <path>`; user extensions live under `~/.run/extensions`. Project extension discovery requires `--project-extensions` and the configured project trust decision.

```python
def setup(api):
    api.add_prompt_guideline("Follow the repository's existing conventions.")

    def started(event, context):
        context.ui.set_status("mode", "Project assistant")

    api.on("session_start", started)
```

Register tools with `api.register_tool(tool)`, commands with `api.register_command(...)`, and hooks with `api.on(event_name, handler)`. Existing hooks include `input`, `before_agent_start`, `context`, tool call/result hooks, `session_start`, `session_shutdown`, and Session events. See `run_agent_coding/extensions/api.py` for exact payload types.

`agent_settled` is emitted after final session entries and execution outcome commit. It carries `run_id`, `session_id`, `branch_id`, `status`, `head_id` and `watermark`. `agent_end` marks the model/tool loop boundary and does not itself prove a durable final result. A failed completion transaction does not emit `agent_settled`.

The UI contract is textual:

- `context.ui.notify(text)` displays a notification.
- `await context.ui.select(title, options)` returns a selection or `None`.
- `await context.ui.confirm(title, message)` returns true only after confirmation.
- `await context.ui.input(title, secret=False)` returns text or `None`; secret input is hidden and does not enter terminal history.
- `context.ui.set_status(key, text)` updates an extension-owned status line; passing `None` removes it.

Headless hosts return `None` or false for dialogs. Extensions must not treat unavailable confirmation as approval. Extensions cannot mount terminal widgets or intercept keys.

Registration failures remove source-owned registrations. Reload retires the old extension generation and clears status displays. Captured APIs from a retired generation reject mutations. This is a lifecycle boundary, not an operating-system sandbox; Python extensions run with the host user's privileges.

All four built-ins in `run_agent_extensions` load by default in new CLI and Gateway sessions: `experience`, `mcp`, `permission_policy` and `plan_mode`. Plan mode starts off, and MCP tools require configured servers. `--no-extensions` disables default and discovered extensions; explicit `run --extension <name-or-path>` entries still load without duplicates. Saved sessions retain their extension snapshot until `--refresh-resources` adopts current defaults. Event tracing is a session option (`--trace`, `/trace`) rather than an extension.

`inference.complete` accepts existing tool schemas by name and returns proposed calls without executing them or altering the main transcript. The experience extension drives its own bounded review loop and applies calls through its allowlist and mutation guards.

Experience keeps `USER.md` and `MEMORY.md` as budgeted, threat-scanned, drift-guarded Markdown entries. `skill_manage` uses ownership/permission checks, advisory lint, usage records and an audit ledger with rollback; optional Skill content scanning defaults off. Review listens only to committed `agent_settled` events and is conditional on cadence or signals, rather than guaranteed after every task. Defaults include a Skill nudge after 10 tool starts and memory cadence of 10 user turns. The native/JSON review loop defaults to at most 16 model requests and 16 tool calls, with 600,000 aggregate input tokens; legacy memory/skills batch compatibility is outside that tool-call counter (see the experience README). New user input cancels review with a bounded acknowledgement wait. The curator ages and recoverably archives managed Skills; model consolidation is opt-in. `/learn` authors a Skill from named sources. See `run_agent_extensions/experience/README.md` for the exact trigger and configuration boundaries.

The Feishu gateway (`run gateway`) is not an extension family: it is a host that loads the same Session extensions with `--extension`. Do not start a channel listener from a Session extension.
