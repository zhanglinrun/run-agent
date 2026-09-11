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

Headless hosts return `None` or false for dialogs. Extensions must not treat unavailable confirmation as approval. Textual widgets, sidebar objects and key interception are no longer extension APIs.

Registration failures remove source-owned registrations. Reload retires the old extension generation and clears status displays. Captured APIs from a retired generation reject mutations. This is a lifecycle boundary, not an operating-system sandbox; Python extensions run with the host user's privileges.

MCP, plan mode, permission policy, verification and observability remain optional extensions in the repository. The experience and managed-task service integration is still being implemented, so do not treat its APIs as finished.

Gateway channels are a separate extension family: export `setup_gateway(api)` and let the Gateway own adapter startup and shutdown. Do not start a channel listener from each Session extension.
