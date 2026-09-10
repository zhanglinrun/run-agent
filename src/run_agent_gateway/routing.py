"""Collision-free route encoding; identity is resolved by a trusted adapter."""

from __future__ import annotations

from dataclasses import asdict

from run_agent_coding.storage.sessions import canonical_json
from run_agent_gateway.contracts import RouteIdentity


def route_key(route: RouteIdentity) -> str:
    for required in (route.adapter_instance_id, route.account_id, route.chat_id):
        if not required or len(required.encode()) > 256:
            raise ValueError("Adapter, account and chat identities must be nonempty and bounded")
    for field in (route.thread_id, route.subject_id):
        if len(field.encode()) > 256:
            raise ValueError("Thread and subject identities must be bounded")
    return canonical_json(asdict(route))
