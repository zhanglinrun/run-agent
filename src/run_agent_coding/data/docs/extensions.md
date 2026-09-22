# Session extensions

A Session extension is a Python module exporting synchronous `setup(api)`. Explicit files load with `run --extension <path>`; user extensions live under `~/.run/extensions`. Project extension discovery requires `--project-extensions` and the configured project trust decision.

```python
def setup(api):
    api.add_prompt_guideline("Follow the repository's existing conventions.")

    def started(event, context):
        context.ui.set_status("mode", "Project assistant")

    api.on("session_start", started)
```

Register tools with `api.register_tool(tool)`, commands with `api.register_command(...)`, and hooks with `api.on(event_name, handler)`. `api.on` accepts Pi's 36 event names (21 observation + 15 hooks). Existing hooks include `input`, `before_agent_start`, `context`, tool call/result hooks, `message_end`, `session_start`, `session_shutdown`, and the other Pi session/provider events. See `run_agent_coding/extensions/api.py` for exact payload types.

`agent_settled` is emitted after final session entries and execution outcome commit. It carries `run_id`, `session_id`, `branch_id`, `status`, `head_id` and `watermark`. `agent_end` marks the model/tool loop boundary and does not itself prove a durable final result. A failed completion transaction does not emit `agent_settled`.

The UI contract is textual:

- `context.ui.notify(text)` displays a notification.
- `await context.ui.select(title, options)` returns a selection or `None`.
- `await context.ui.confirm(title, message)` returns true only after confirmation.
- `await context.ui.input(title, secret=False)` returns text or `None`; secret input is hidden and does not enter terminal history.
- `context.ui.set_status(key, text)` updates an extension-owned status line; passing `None` removes it.

Headless hosts return `None` or false for dialogs. Extensions must not treat unavailable confirmation as approval. Extensions cannot mount terminal widgets or intercept keys.

Registration failures remove source-owned registrations. Reload and session replacement stage a successor without touching the live runtime. Only after host publication commits does the old generation enter a read-only retiring phase, receive shutdown notification, clear its status, and drain source-owned disposers in reverse order. Captured APIs from a retiring or retired generation reject mutations. This is lifecycle management, not an operating-system sandbox; trusted Python extensions can still perform direct effects the host cannot reverse.

All four built-ins in `run_agent_extensions` load by default in new CLI sessions: `experience`, `mcp`, `permission_policy` and `plan_mode`. Plan mode starts off. Permission uses `RUN_AGENT_PERMISSION_MODE` (`review` default, `yolo`/`autonomous`, `strict`) and does not register tools. MCP reads `<cwd>/.mcp.json`, connects on `session_start`, and releases connections through a source-owned disposer. `--no-extensions` disables default and discovered extensions; explicit `run --extension <name-or-path>` entries still load without duplicates. Saved sessions retain their extension snapshot until `--refresh-resources` adopts current defaults. Event tracing is a session option (`--trace`, `/trace`) rather than an extension.

`inference.complete` accepts existing tool schemas by name and returns proposed calls without executing them or altering the main transcript. `evaluation` is host-owned: it is unavailable by default and never treats missing evidence as success.

Experience keeps `USER.md` and `MEMORY.md` as budgeted, threat-scanned Markdown entries. Formal Skills are read-only to the model: `skill_manage propose` creates an isolated cold candidate bound to a committed run, base digest, bounded edit operations and optional project probes. A candidate enters the normal Skill loader only after the host evaluator measures the frozen candidate and `/evolve publish` rechecks the report, probes, ownership, pin and base digest. There is no cadence-driven writeback or automatic archive/consolidation pass. See `run_agent_extensions/experience/README.md` for the exact gate.
