"""Show source-owned textual status in the unified terminal."""


def setup(api):
    completed = 0

    def started(event, context):
        context.ui.set_status("turns", f"Completed: {completed}")

    def settled(event, context):
        nonlocal completed
        if event.status == "succeeded":
            completed += 1
        context.ui.set_status("turns", f"Completed: {completed}")

    api.on("session_start", started)
    api.on("agent_settled", settled)
