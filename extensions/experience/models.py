"""Experience namespace values; no database or Gateway dependency."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AssetKind = Literal["user", "memory", "skill"]
Scope = Literal["project", "user"]


def asset_key(kind: AssetKind, name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError("Asset names use 1-64 lowercase letters, digits, underscores or hyphens")
    return f"{kind}/{name}"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    candidate_id: str
    asset_id: str
    kind: AssetKind
    scope: Scope
    base_version: str | None
    content_version: str
    content_hash: str
    source_session: str
    source_kind: Literal["manual", "model"]
    source_command: str | None = None
    source_snapshot: str | None = None
    source_run: str | None = None
    observed_at: float
    applies_to: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    status: Literal["proposed", "needs_evidence", "promoted", "rejected", "stale"] = "proposed"
    report_id: str | None = None


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Scope = "project"
    kind: AssetKind = "memory"
    name: str
    content: str = Field(min_length=1, max_length=24000)
    description: str = Field(default="", max_length=512)
    applies_to: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    expires_at: float | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        asset_key("memory", name)
        return name

    @field_validator("content")
    @classmethod
    def valid_content(cls, content: str) -> str:
        content = content.strip()
        if not content or "\x00" in content or len(content.encode()) > 64000:
            raise ValueError("Experience content must be nonempty text within 64 KiB")
        return content
