from __future__ import annotations

import json
import re
import time
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .frontmatter import format_frontmatter, parse_frontmatter
from .json_utils import parse_json_object as _parse_json_object
from ..runtime.scope import current_workspace


USAGE_LOG = "usage.jsonl"
ONLINE_PROVENANCE_LOG = "online_provenance.jsonl"
ONLINE_PROVENANCE_INDEX = "online_skill_provenance.json"
SKILL_USAGE_STATS = "skill_usage_stats.json"
HISTORY_DIR = "history"


def get_evolution_dir() -> Path:
    return current_workspace() / ".run" / "skill-evolution"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_skill_slug(name: str) -> str:
    raw = str(name or "").strip()
    slug = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if slug:
        return slug[:120].rstrip("-.") or slug[:120]
    if raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"skill-{digest}"
    return "unknown"


def _preview(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _compact_messages(messages: list[dict[str, Any]], *, max_messages: int = 12, max_chars: int = 4000) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in list(messages or [])[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        out.append({"role": role, "content": _preview(content, max_chars)})
    return out


def record_skill_invocation(
    *,
    skill_name: str,
    source: str,
    context: str,
    args: object = "",
) -> None:
    row = {
        "event": "invoke",
        "time": _utc_now(),
        "skill": skill_name,
        "source": source,
        "context": context,
        "args_preview": _preview(args),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_skill_feedback(
    *,
    skill_name: str,
    rating: str,
    note: str = "",
) -> None:
    row = {
        "event": "feedback",
        "time": _utc_now(),
        "skill": skill_name,
        "rating": str(rating or "").strip(),
        "note": _preview(note, 1200),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_online_skill_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    row = {
        "event": "online_ingest",
        "time": _utc_now(),
        "action": str(action or "none").strip() or "none",
        "skill": str(skill_name or "").strip(),
        "ok": bool((result or {}).get("ok")) if result is not None else not bool(error),
        "result": result or {},
        "messages": _compact_messages(list(messages or [])),
        "retrieved_reference": retrieved_reference or {},
        "decision": decision or {},
        "error": _preview(error, 1200),
    }
    _append_jsonl(get_evolution_dir() / ONLINE_PROVENANCE_LOG, row)
    _update_online_provenance_index(row)


def _update_online_provenance_index(row: dict[str, Any]) -> None:
    skill = str(row.get("skill") or "").strip()
    if not skill:
        return
    root = get_evolution_dir()
    path = root / ONLINE_PROVENANCE_INDEX
    index = _read_json(path, {})
    item = index.setdefault(
        skill,
        {
            "skill": skill,
            "source_count": 0,
            "history_count": 0,
            "sources": [],
            "version_timeline": [],
            "usage": {},
        },
    )
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    item["current_version"] = result.get("version") or item.get("current_version", "")
    item["last_action"] = row.get("action")
    item["last_time"] = row.get("time")
    item["last_ok"] = row.get("ok")
    item["last_error"] = row.get("error", "")
    item["source_count"] = int(item.get("source_count", 0)) + 1
    source = {
        "time": row.get("time"),
        "action": row.get("action"),
        "ok": row.get("ok"),
        "messages": row.get("messages", []),
        "retrieved_reference": row.get("retrieved_reference", {}),
        "decision": row.get("decision", {}),
        "error": row.get("error", ""),
    }
    sources = list(item.get("sources") or [])
    sources.append(source)
    item["sources"] = sources[-20:]
    item["history_count"] = len(sources)
    if result.get("version"):
        timeline = list(item.get("version_timeline") or [])
        timeline.append({"time": row.get("time"), "version": result.get("version"), "action": row.get("action")})
        item["version_timeline"] = timeline[-50:]
    index[skill] = item
    _write_json(path, index)


def _parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _bump_patch(version: str | None) -> str:
    raw = str(version or "0.1.0").strip()
    parts = raw.split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return "0.1.1"
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts[:3])


def _find_skill_file_by_name(base_dir: Path, skill_name: str) -> Path | None:
    if not base_dir.is_dir():
        return None
    wanted = str(skill_name or "").strip()
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            parsed = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = parsed.meta.get("name") or entry.name
        if name == wanted:
            return skill_file
    return None


def resolve_skill_file(skill_name: str, *, target: str = "active", active_dir: str = "") -> Path | None:
    target = (target or "active").strip().lower()
    if target == "active" and active_dir:
        skill_file = Path(active_dir) / "SKILL.md"
        if skill_file.is_file():
            return skill_file

    if target in ("project", "active"):
        found = _find_skill_file_by_name(current_workspace() / ".run" / "skills", skill_name)
        if found:
            return found

    if target in ("user", "active"):
        found = _find_skill_file_by_name(Path.home() / ".run" / "skills", skill_name)
        if found:
            return found

    return None


def _skills_root(target: str) -> Path:
    target = (target or "project").strip().lower()
    if target == "user":
        return Path.home() / ".run" / "skills"
    return current_workspace() / ".run" / "skills"


def _normalize_context(context: str | None) -> str:
    return "fork" if str(context or "").strip().lower() == "fork" else "inline"


def _normalize_allowed_tools(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ",".join(part.strip() for part in str(value).split(",") if part.strip())


def _skill_body(instructions: str, evidence: str = "") -> str:
    body = str(instructions or "").strip()
    if not body:
        body = (
            "# Goal\n\n"
            "Apply this reusable skill when the user's request matches the trigger.\n\n"
            "# Workflow\n\n"
            "1. Identify the user's concrete task and constraints.\n"
            "2. Follow the reusable rules captured in this skill.\n"
            "3. Produce the requested output directly.\n"
        )
    if not body.lstrip().startswith("#"):
        body = "# Skill Instructions\n\n" + body
    if evidence:
        body = body.rstrip() + "\n\n## Creation Evidence\n\n" + str(evidence).strip() + "\n"
    return body.rstrip() + "\n"


def _operation_ids(meta: dict[str, Any]) -> list[str]:
    return [
        item.strip()
        for item in str(meta.get("applied-operations") or "").split(",")
        if item.strip()
    ]


def _operation_applied(skill_file: Path, operation_id: str) -> bool:
    if not operation_id or not skill_file.is_file():
        return False
    try:
        parsed = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    return operation_id in _operation_ids(dict(parsed.meta))


def _record_operation(meta: dict[str, Any], operation_id: str) -> None:
    if not operation_id:
        return
    operations = _operation_ids(meta)
    if operation_id not in operations:
        operations.append(operation_id)
    meta["applied-operations"] = ",".join(operations[-20:])


def create_skill_file(
    *,
    name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    target: str = "project",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: object = None,
    evidence: str = "",
    actor: str = "agent",
    tags: list[str] | None = None,
    operation_id: str = "",
) -> dict[str, Any]:
    resolved_name = str(name or "").strip()
    if not resolved_name:
        return {"ok": False, "error": "skill name is required"}
    if not str(description or "").strip():
        return {"ok": False, "error": "description is required"}

    root = _skills_root(target)
    skill_dir = root / _safe_skill_slug(resolved_name)
    skill_file = skill_dir / "SKILL.md"
    operation_id = str(operation_id or "").strip()
    if _operation_applied(skill_file, operation_id):
        return {
            "ok": True,
            "event": "create",
            "skill": resolved_name,
            "file": str(skill_file),
            "operation_id": operation_id,
            "idempotent": True,
        }

    existing = resolve_skill_file(resolved_name, target="active")
    if existing:
        return {"ok": False, "error": f"Skill already exists: {resolved_name}", "file": str(existing)}
    if skill_file.exists():
        return {"ok": False, "error": f"Skill file already exists: {skill_file}"}

    meta = {
        "name": resolved_name,
        "description": re.sub(r"\s+", " ", str(description).strip()),
        "version": "0.1.0",
        "created-at": _utc_now(),
        "user-invocable": "true" if user_invocable else "false",
        "context": _normalize_context(context),
    }
    if when_to_use:
        meta["when-to-use"] = re.sub(r"\s+", " ", str(when_to_use).strip())
    if tags:
        meta["tags"] = ",".join(re.sub(r"\s+", "-", str(tag).strip()) for tag in tags if str(tag).strip())
    tools = _normalize_allowed_tools(allowed_tools)
    if tools:
        meta["allowed-tools"] = tools
    _record_operation(meta, operation_id)

    skill_dir.mkdir(parents=True, exist_ok=False)
    body = _skill_body(instructions, evidence)
    skill_file.write_text(format_frontmatter(meta, body), encoding="utf-8")

    event = {
        "event": "create",
        "time": _utc_now(),
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "target": "user" if (target or "").strip().lower() == "user" else "project",
        "description": _preview(description, 1200),
        "when_to_use": _preview(when_to_use, 1200),
        "evidence": _preview(evidence, 1200),
        "operation_id": operation_id,
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    return {"ok": True, **event}


def _append_evolution_note(body: str, lesson: str, rationale: str = "") -> str:
    lesson = re.sub(r"\s+", " ", str(lesson or "").strip())
    rationale = re.sub(r"\s+", " ", str(rationale or "").strip())
    if not lesson:
        raise ValueError("lesson is required")

    bullet = f"- {_today()}: {lesson}"
    if rationale:
        bullet += f" Reason: {rationale}"

    body = str(body or "").rstrip()
    marker = "## Evolution Notes"
    if marker in body:
        if lesson in body:
            return body + "\n"
        return body + "\n" + bullet + "\n"
    return body + "\n\n" + marker + "\n\n" + bullet + "\n"


def evolve_skill_file(
    *,
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    active_dir: str = "",
    actor: str = "agent",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
    operation_id: str = "",
) -> dict[str, Any]:
    skill_file = resolve_skill_file(skill_name, target=target, active_dir=active_dir)
    if not skill_file:
        return {"ok": False, "error": f"Skill not found: {skill_name}"}

    raw = skill_file.read_text(encoding="utf-8")
    parsed = parse_frontmatter(raw)
    meta = dict(parsed.meta)
    resolved_name = meta.get("name") or skill_file.parent.name
    operation_id = str(operation_id or "").strip()
    if operation_id in _operation_ids(meta):
        return {
            "ok": True,
            "event": "evolve",
            "skill": resolved_name,
            "file": str(skill_file),
            "version": meta.get("version", ""),
            "operation_id": operation_id,
            "idempotent": True,
        }

    snapshot = {
        "time": _utc_now(),
        "event": "snapshot",
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "version": meta.get("version", "0.1.0"),
        "lesson": _preview(lesson, 1200),
        "rationale": _preview(rationale, 1200),
        "content": raw,
    }
    history_path = get_evolution_dir() / HISTORY_DIR / f"{_safe_skill_slug(resolved_name)}.jsonl"
    _append_jsonl(history_path, snapshot)

    meta["name"] = resolved_name
    meta["version"] = _bump_patch(meta.get("version"))
    meta["last-evolved"] = _utc_now()
    meta["evolution-count"] = str(_parse_int(meta.get("evolution-count"), 0) + 1)
    _record_operation(meta, operation_id)
    if description.strip():
        meta["description"] = re.sub(r"\s+", " ", description.strip())
    if when_to_use.strip():
        meta["when-to-use"] = re.sub(r"\s+", " ", when_to_use.strip())
    if tags:
        existing_tags = [part.strip() for part in str(meta.get("tags") or "").split(",") if part.strip()]
        merged_tags = existing_tags[:]
        for tag in tags:
            normalized = re.sub(r"\s+", "-", str(tag).strip())
            if normalized and normalized not in merged_tags:
                merged_tags.append(normalized)
        if merged_tags:
            meta["tags"] = ",".join(merged_tags[:12])

    if instructions.strip():
        new_body = _skill_body(instructions.strip())
        note = _append_evolution_note("", lesson, rationale).strip()
        if note:
            new_body = new_body.rstrip() + "\n\n" + note + "\n"
    else:
        new_body = _append_evolution_note(parsed.body, lesson, rationale)
    skill_file.write_text(format_frontmatter(meta, new_body), encoding="utf-8")

    event = {
        "event": "evolve",
        "time": _utc_now(),
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "version": meta["version"],
        "target": target,
        "lesson": _preview(lesson, 1200),
        "rationale": _preview(rationale, 1200),
        "history": str(history_path),
        "operation_id": operation_id,
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    return {"ok": True, **event}


def record_skill_usage_judgments(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    stats_path = get_evolution_dir() / SKILL_USAGE_STATS
    stats = _read_json(stats_path, {})
    if not isinstance(stats, dict):
        stats = {}
    for judgment in judgments:
        skill = str(judgment.get("name") or judgment.get("skill") or "").strip()
        if not skill:
            continue
        item = stats.setdefault(
            skill,
            {
                "retrieved": 0,
                "relevant": 0,
                "used": 0,
                "last_retrieved": "",
                "last_used": "",
                "source": judgment.get("source", ""),
                "skill_dir": judgment.get("skill_dir", ""),
            },
        )
        item["retrieved"] = int(item.get("retrieved", 0)) + 1
        item["last_retrieved"] = _utc_now()
        item["source"] = judgment.get("source", item.get("source", ""))
        item["skill_dir"] = judgment.get("skill_dir", item.get("skill_dir", ""))
        if judgment.get("relevant"):
            item["relevant"] = int(item.get("relevant", 0)) + 1
        if judgment.get("used"):
            item["used"] = int(item.get("used", 0)) + 1
            item["last_used"] = _utc_now()
        item["last_reason"] = _preview(judgment.get("reason", ""), 500)
        item["last_score"] = judgment.get("score", 0)
    _write_json(stats_path, stats)
    return {"ok": True, "judgments": len(judgments)}


def load_skill_stats() -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    usage_path = get_evolution_dir() / USAGE_LOG
    if usage_path.is_file():
        for line in usage_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            skill = str(row.get("skill") or "").strip()
            if not skill:
                continue
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            event = row.get("event")
            if event == "create":
                item["created"] = int(item.get("created", 0)) + 1
                item["created_at"] = row.get("time")
                item["file"] = row.get("file")
            elif event == "invoke":
                item["invocations"] = int(item.get("invocations", 0)) + 1
                item["last_invoked"] = row.get("time")
            elif event == "feedback":
                item["feedback"] = int(item.get("feedback", 0)) + 1
                item["last_feedback"] = row.get("time")
            elif event == "evolve":
                item["evolutions"] = int(item.get("evolutions", 0)) + 1
                item["last_evolved"] = row.get("time")
                item["version"] = row.get("version")
                item["file"] = row.get("file")

    history_root = get_evolution_dir() / HISTORY_DIR
    if history_root.is_dir():
        for path in history_root.glob("*.jsonl"):
            count = len([line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
            skill = path.stem
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["snapshots"] = count
    usage_stats = _read_json(get_evolution_dir() / SKILL_USAGE_STATS, {})
    if isinstance(usage_stats, dict):
        for skill, usage in usage_stats.items():
            if not isinstance(usage, dict):
                continue
            item = stats.setdefault(str(skill), {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["retrieved"] = usage.get("retrieved", 0)
            item["relevant"] = usage.get("relevant", 0)
            item["used"] = usage.get("used", 0)
            item["last_retrieved"] = usage.get("last_retrieved", "")
            item["last_used"] = usage.get("last_used", "")
    return stats


def format_skill_stats() -> str:
    stats = load_skill_stats()
    if not stats:
        return "No skill evolution events recorded yet."

    lines = ["Skill evolution stats:"]
    for name in sorted(stats):
        item = stats[name]
        parts = [
            f"created={item.get('created', 0)}",
            f"invoked={item.get('invocations', 0)}",
            f"feedback={item.get('feedback', 0)}",
            f"evolved={item.get('evolutions', 0)}",
            f"snapshots={item.get('snapshots', 0)}",
            f"retrieved={item.get('retrieved', 0)}",
            f"used={item.get('used', 0)}",
        ]
        if item.get("created_at"):
            parts.append(f"created_at={item['created_at']}")
        if item.get("version"):
            parts.append(f"version={item['version']}")
        if item.get("last_invoked"):
            parts.append(f"last_invoked={item['last_invoked']}")
        if item.get("last_used"):
            parts.append(f"last_used={item['last_used']}")
        lines.append(f"  {name}: " + ", ".join(parts))
    return "\n".join(lines)


# Online feedback candidate extraction and maintenance

SideQuery = Callable[[str, str], Awaitable[str]]
ConfirmWrite = Callable[[str], Awaitable[bool]]


@dataclass
class OnlineSkillCandidate:
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)


def _normalize_identity(text: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", raw).strip()


def _candidate_search_text(candidate: OnlineSkillCandidate) -> str:
    return "\n".join(
        [
            candidate.name,
            candidate.description,
            candidate.when_to_use,
            candidate.instructions,
            " ".join(candidate.tags),
        ]
    )


def _coerce_candidate(obj: dict[str, Any]) -> OnlineSkillCandidate | None:
    name = str(obj.get("name") or "").strip()
    description = str(obj.get("description") or "").strip()
    instructions = str(obj.get("instructions") or obj.get("prompt") or "").strip()
    if not name or not description or not instructions:
        return None
    tags_raw = obj.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [part.strip() for part in re.split(r"[,，]", tags_raw) if part.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(part).strip() for part in tags_raw if str(part).strip()]
    else:
        tags = []
    return OnlineSkillCandidate(
        name=name,
        description=description,
        when_to_use=str(obj.get("when_to_use") or obj.get("when-to-use") or "").strip(),
        instructions=instructions,
        evidence=str(obj.get("evidence") or "").strip(),
        tags=tags[:8],
    )


async def extract_online_skill_candidate(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineSkillCandidate | None:
    system = (
        "You are Run Agent's online Skill Extractor.\n"
        "Extract at most ONE reusable skill candidate from a live conversation window.\n"
        "Output ONLY strict JSON: {\"skills\": []} or {\"skills\": [{...}]}.\n\n"
        "Candidate fields: name, description, when_to_use, instructions, evidence, tags.\n\n"
        "Rules:\n"
        "- USER turns are the primary evidence. Assistant turns are context only.\n"
        "- A next user feedback turn may confirm, reject, or refine the prior assistant behavior.\n"
        "- Do not extract assistant-only guesses, weak confirmations, one-off task payload, secrets, project facts, URLs, account IDs, exact dates, or temporary parameters.\n"
        "- Extract only durable workflow, output policy, implementation preference, correction, or repeated constraint likely useful for future similar tasks.\n"
        "- Remove entity names and runtime-specific payload; use placeholders where needed.\n"
        "- retrieved_reference is identity context only; never treat it as new user evidence.\n"
        "- If evidence is weak, generic, or low-value, return {\"skills\": []}.\n"
    )
    payload = {
        "messages": messages,
        "hint": hint,
        "retrieved_reference": retrieved_reference or None,
    }
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not skills:
        return None
    first = skills[0]
    if not isinstance(first, dict):
        return None
    return _coerce_candidate(first)


def _exact_identity_match(candidate: OnlineSkillCandidate, skills: list[Any]) -> str:
    candidate_ids = {
        _normalize_identity(candidate.name),
        _normalize_identity(candidate.description),
        _normalize_identity(candidate.when_to_use),
    }
    candidate_ids.discard("")
    for skill in skills:
        skill_ids = {
            _normalize_identity(getattr(skill, "name", "")),
            _normalize_identity(getattr(skill, "description", "")),
            _normalize_identity(getattr(skill, "when_to_use", "") or ""),
        }
        skill_ids.discard("")
        if candidate_ids & skill_ids:
            return getattr(skill, "name", "")
    return ""


async def maintain_online_skill_candidate(
    *,
    candidate: OnlineSkillCandidate,
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import discover_skills, retrieve_relevant_skills

    skills = discover_skills()
    exact_target = _exact_identity_match(candidate, skills)
    similar_hits = retrieve_relevant_skills(_candidate_search_text(candidate), limit=8, min_score=0.03)
    top_reference_name = str((retrieved_reference or {}).get("name") or "").strip()

    system = (
        "You are Run Agent's online Skill Set Manager.\n"
        "Decide whether a candidate should add a new skill, merge into an existing skill, or be discarded.\n"
        "Output ONLY strict JSON.\n\n"
        "Schema:\n"
        "{\"action\":\"add|merge|discard\",\"target_skill\":\"existing name for merge\","
        "\"reason\":\"short reason\",\"merged_description\":\"optional\","
        "\"merged_when_to_use\":\"optional\",\"merged_instructions\":\"optional full merged SKILL.md body\"}\n\n"
        "Rules:\n"
        "- Prefer merge over add when the same capability already exists.\n"
        "- Discard if the candidate duplicates an existing shared/project skill and adds no user-specific durable improvement.\n"
        "- If merging, synthesize a complete merged instruction body, preserving useful existing guidance and adding only durable new guidance.\n"
        "- Do not preserve one-off payload, secrets, transient project facts, URLs, exact dates, or assistant-only claims.\n"
    )
    payload = {
        "candidate": asdict(candidate),
        "exact_identity_target": exact_target,
        "retrieved_reference": retrieved_reference or None,
        "similar_skills": similar_hits,
        "existing_skills": [
            {
                "name": getattr(skill, "name", ""),
                "description": getattr(skill, "description", ""),
                "when_to_use": getattr(skill, "when_to_use", "") or "",
                "source": getattr(skill, "source", ""),
                "context": getattr(skill, "context", ""),
                "instructions": (getattr(skill, "prompt_template", "") or "")[:6000],
            }
            for skill in skills[:80]
        ],
    }

    decision = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    action = str(decision.get("action") or "").strip().lower()
    target_skill = str(decision.get("target_skill") or "").strip()

    if exact_target:
        action = "merge"
        target_skill = exact_target
    elif action == "add" and similar_hits:
        top = similar_hits[0]
        if float(top.get("score", 0.0)) >= 0.55:
            action = "merge"
            target_skill = str(top.get("name") or "")
    elif action == "merge" and not target_skill:
        target_skill = top_reference_name

    if action not in {"add", "merge", "discard"}:
        action = "discard"

    if action == "discard":
        return {"ok": True, "action": "discard", "skill": "", "decision": decision}

    write_summary = f"online skill evolution: {action} {target_skill or candidate.name}"
    if confirm_write is not None and not await confirm_write(write_summary):
        return {
            "ok": False,
            "action": f"{action}_denied",
            "skill": target_skill or candidate.name,
            "error": "permission denied",
            "decision": decision,
        }

    if action == "merge":
        target_skill = target_skill or top_reference_name
        if not target_skill:
            return {"ok": False, "action": "merge", "error": "missing target_skill", "decision": decision}
        from .candidates import stage_skill_candidate

        proposed = asdict(candidate)
        proposed.update(
            {
                "instructions": str(decision.get("merged_instructions") or candidate.instructions),
                "description": str(decision.get("merged_description") or candidate.description),
                "when_to_use": str(decision.get("merged_when_to_use") or candidate.when_to_use),
                "rationale": str(decision.get("reason") or "Online maintainer merge"),
            }
        )
        target_scope = next(
            (
                str(getattr(skill, "source", ""))
                for skill in skills
                if getattr(skill, "name", "") == target_skill
                and str(getattr(skill, "source", "")) in {"project", "user"}
            ),
            target,
        )
        result = stage_skill_candidate(
            candidate=proposed,
            proposed_action="merge",
            target_skill=target_skill,
            decision=decision,
            target=target_scope,
        )
        return {"proposed_action": "merge", "candidate": asdict(candidate), "decision": decision, **result}

    from .candidates import stage_skill_candidate

    result = stage_skill_candidate(
        candidate=asdict(candidate),
        proposed_action="add",
        decision=decision,
        target=target,
    )
    return {"proposed_action": "add", "candidate": asdict(candidate), "decision": decision, **result}


async def online_ingest(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    from .skills import record_online_provenance

    try:
        candidate = await extract_online_skill_candidate(
            messages=messages,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            hint=hint,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "error": str(exc)}
        record_online_provenance(
            action="failed",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
            error=str(exc),
        )
        return result

    if candidate is None:
        result = {"ok": True, "action": "none"}
        record_online_provenance(
            action="none",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
        )
        return result

    try:
        result = await maintain_online_skill_candidate(
            candidate=candidate,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            confirm_write=confirm_write,
            target=target,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}

    record_online_provenance(
        action=str(result.get("action") or "none"),
        skill_name=str(result.get("skill") or candidate.name),
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        error="" if result.get("ok") else str(result.get("error") or ""),
    )
    return result
