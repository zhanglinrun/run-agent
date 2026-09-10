"""Durable gateway identities, admission limits and execution qualifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from run_agent_core.types import JSONValue

Lane = Literal["foreground", "background"]


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    adapter_instance_id: str
    account_id: str
    chat_id: str
    thread_id: str = ""
    subject_id: str = ""


@dataclass(frozen=True, slots=True)
class Submission:
    route: RouteIdentity
    principal_id: str
    source_message_id: str
    content: str
    workspace: Path
    lane: Lane = "foreground"
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    mode: Literal["queue", "steer"] = "queue"


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    task_id: str
    session_id: str
    conversation_epoch: int
    sequence: int
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    waiting_total: int = 1024
    waiting_foreground_reserved: int = 256
    waiting_background_reserved: int = 64
    per_session: int = 16
    per_principal: int = 128
    background_roots_per_principal: int = 4
    payload_bytes: int = 65536
    outbox_pending: int = 4096
    running_total: int = 8
    running_foreground_reserved: int = 4
    running_background_reserved: int = 1

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.waiting_total,
                self.per_session,
                self.per_principal,
                self.background_roots_per_principal,
                self.payload_bytes,
                self.outbox_pending,
                self.running_total,
            )
        ):
            raise ValueError("Gateway limits must be positive")
        for total, foreground, background in (
            (
                self.waiting_total,
                self.waiting_foreground_reserved,
                self.waiting_background_reserved,
            ),
            (
                self.running_total,
                self.running_foreground_reserved,
                self.running_background_reserved,
            ),
        ):
            if foreground < 1 or background < 1 or foreground + background > total:
                raise ValueError("Each lane needs a reservation within total capacity")


@dataclass(frozen=True, slots=True)
class GatewayOwner:
    owner_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class Assignment:
    task_id: str
    run_id: str
    attempt: int
    generation: int
    session_id: str
    principal_id: str
    lane: Lane
    workspace_id: str
    workspace: Path
    content: str
    metadata: dict[str, JSONValue]


class AdmissionRejected(RuntimeError):
    pass


class DuplicateConflict(RuntimeError):
    pass


class GatewayOwnershipLost(RuntimeError):
    pass
