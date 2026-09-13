"""Interactive cards for the Feishu adapter: the command-approval prompt and its answer.

The layout follows hermes-agent's ``send_exec_approval``: an orange header, the command
in a code block, and buttons whose ``value`` carries the action and the approval id so
the card-action callback can route the click.
"""

from __future__ import annotations

from typing import Any

APPROVAL_CHOICE_MAP: dict[str, str] = {
    "approve_once": "once",
    "approve_session": "session",
    "approve_always": "always",
    "deny": "deny",
}
APPROVAL_LABEL_MAP: dict[str, str] = {
    "once": "Approved once",
    "session": "Approved for session",
    "always": "Approved permanently",
    "deny": "Denied",
}
ACTION_KEY = "run_action"
APPROVAL_KEY = "approval_id"


def _button(label: str, action: str, approval_id: str, kind: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "value": {ACTION_KEY: action, APPROVAL_KEY: approval_id},
    }


def format_approval_body(command: str, description: str) -> str:
    preview = command if len(command) <= 200 else command[:200] + "..."
    return f"```\n{preview}\n```\nReason: {description}"


def build_approval_card(
    *,
    approval_id: str,
    command: str,
    description: str,
    allow_session: bool = True,
    allow_always: bool = True,
) -> dict[str, Any]:
    actions = [_button("✅ Allow Once", "approve_once", approval_id, "primary")]
    if allow_session:
        actions.append(_button("✅ Session", "approve_session", approval_id))
        if allow_always:
            actions.append(_button("✅ Always", "approve_always", approval_id))
    actions.append(_button("❌ Deny", "deny", approval_id, "danger"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "⚠️ Command Approval Required", "tag": "plain_text"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": format_approval_body(command, description)},
            {"tag": "action", "actions": actions},
        ],
    }


def build_resolved_card(*, choice: str, user_name: str) -> dict[str, Any]:
    icon = "❌" if choice == "deny" else "✅"
    label = APPROVAL_LABEL_MAP.get(choice, "Resolved")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": f"{icon} {label}", "tag": "plain_text"},
            "template": "red" if choice == "deny" else "green",
        },
        "elements": [{"tag": "markdown", "content": f"{icon} **{label}** by {user_name}"}],
    }


def parse_approval_action(value: Any) -> tuple[str, str] | None:
    """The (approval_id, choice) a button carried, or None for any other card action."""
    if not isinstance(value, dict):
        return None
    action = value.get(ACTION_KEY)
    approval_id = value.get(APPROVAL_KEY)
    if not isinstance(action, str) or approval_id is None:
        return None
    choice = APPROVAL_CHOICE_MAP.get(action)
    if choice is None:
        return None
    return str(approval_id), choice


__all__ = [
    "ACTION_KEY",
    "APPROVAL_CHOICE_MAP",
    "APPROVAL_KEY",
    "APPROVAL_LABEL_MAP",
    "build_approval_card",
    "build_resolved_card",
    "format_approval_body",
    "parse_approval_action",
]
