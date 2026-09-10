"""Host-configured mapping of verified channel identities to principals and workspaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from run_agent_gateway.contracts import RouteIdentity


@dataclass(frozen=True, slots=True)
class IdentityRule:
    adapter_instance_id: str
    account_id: str
    sender_id: str
    principal_id: str
    workspace: Path
    conversation_scope: Literal["sender", "shared"] = "sender"


class IdentityPolicy:
    def __init__(self, rules: tuple[IdentityRule, ...]) -> None:
        self._rules: dict[tuple[str, str, str], IdentityRule] = {}
        for rule in rules:
            fields = (rule.adapter_instance_id, rule.account_id, rule.sender_id, rule.principal_id)
            if any(not value or len(value.encode()) > 256 for value in fields):
                raise ValueError("Identity fields must contain 1 to 256 UTF-8 bytes")
            if rule.conversation_scope not in {"sender", "shared"}:
                raise ValueError("Conversation scope must be sender or shared")
            if not rule.workspace.is_dir():
                raise ValueError(f"Gateway workspace is not a directory: {rule.workspace}")
            key = (rule.adapter_instance_id, rule.account_id, rule.sender_id)
            if key in self._rules:
                raise ValueError("Duplicate channel identity mapping")
            self._rules[key] = rule

    @classmethod
    def load(cls, path: Path) -> IdentityPolicy:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, list):
            raise ValueError("Identity map must be an array of explicit channel mappings")
        rules = []
        for item in document:
            if not isinstance(item, dict):
                raise ValueError("Identity rule must be an object")
            workspace = Path(item["workspace"]).expanduser()
            if not workspace.is_absolute():
                workspace = path.resolve().parent / workspace
            rules.append(IdentityRule(**{**item, "workspace": workspace.resolve()}))
        return cls(tuple(rules))

    def resolve(
        self, adapter: str, account: str, sender: str, chat: str, thread: str
    ) -> tuple[IdentityRule, RouteIdentity]:
        rule = self._rules.get((adapter, account, sender))
        if rule is None:
            raise PermissionError("Channel identity is not authorized by the Gateway identity map")
        return rule, RouteIdentity(
            adapter, account, chat, thread, sender if rule.conversation_scope == "sender" else ""
        )
