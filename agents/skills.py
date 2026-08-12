"""Skills discovery / retrieval / inline execution (C05)."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter


@dataclass
class SkillDefinition:
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork" (fork = C08)
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""


_cached_skills: list[SkillDefinition] | None = None

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


def execute_skill(skill_name: str, args: object) -> dict | None:
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None
    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
        "source": skill.source,
        "skill_dir": skill.skill_dir,
    }


def resolve_skill_prompt(skill: SkillDefinition, args: object) -> str:
    prompt = skill.prompt_template
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", str(args or ""), prompt)
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt


def get_skill_by_name(skill_name: str) -> SkillDefinition | None:
    for skill in discover_skills():
        if skill.name == skill_name:
            return skill
    return None


def discover_skills() -> list[SkillDefinition]:
    global _cached_skills
    if _cached_skills is not None:
        return _cached_skills

    skills: dict[str, SkillDefinition] = {}
    user_dir = Path.home() / ".run" / "skills"
    project_dir = Path.cwd() / ".run" / "skills"
    _load_skills_from_dir(user_dir, "user", skills, overwrite=True)
    _load_skills_from_dir(project_dir, "project", skills, overwrite=False)

    _cached_skills = list(skills.values())
    return _cached_skills


def _load_skills_from_dir(
    base_dir: Path,
    source: str,
    skills: dict[str, SkillDefinition],
    *,
    overwrite: bool = True,
) -> None:
    if not base_dir.is_dir():
        return
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry))
        if not skill:
            continue
        if not overwrite and skill.name in skills:
            continue
        skills[skill.name] = skill


def _parse_skill_file(file_path: Path, source: str, skill_dir: str) -> SkillDefinition | None:
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
        result = parse_frontmatter(raw)
        meta = result.meta

        name = meta.get("name") or file_path.parent.name or "unknown"
        user_invocable = meta.get("user-invocable", "true") != "false"
        context = "fork" if meta.get("context") == "fork" else "inline"

        allowed_tools: list[str] | None = None
        if "allowed-tools" in meta:
            raw_tools = meta["allowed-tools"]
            if raw_tools.startswith("["):
                try:
                    allowed_tools = json.loads(raw_tools)
                except Exception:
                    allowed_tools = [s.strip() for s in raw_tools.strip("[]").split(",") if s.strip()]
            else:
                allowed_tools = [s.strip() for s in raw_tools.split(",") if s.strip()]

        return SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed_tools,
            user_invocable=user_invocable,
            context=context,
            prompt_template=result.body,
            source=source,
            skill_dir=skill_dir,
        )
    except Exception:
        return None


def build_skill_descriptions() -> str:
    skills = discover_skills()
    lines = ["# Available Skills", ""]
    if not skills:
        lines.append("(No skills are currently registered.)")
        lines.append("")
    else:
        invocable = [s for s in skills if s.user_invocable]
        auto_only = [s for s in skills if not s.user_invocable]

        if invocable:
            lines.append("User-invocable skills (user types /<name> to invoke):")
            for s in invocable:
                lines.append(f"- **/{s.name}**: {s.description}")
                if s.when_to_use:
                    lines.append(f"  When to use: {s.when_to_use}")
            lines.append("")

        if auto_only:
            lines.append("Auto-invocable skills:")
            lines.append(
                "When the user's request matches a skill's When to use, "
                "call the `skill` tool with that skill name before continuing. "
                "Do not ask the user to invoke it manually."
            )
            for s in auto_only:
                lines.append(f"- **{s.name}**: {s.description}")
                if s.when_to_use:
                    lines.append(f"  When to use: {s.when_to_use}")
            lines.append("")

        lines.append(
            "To invoke a skill programmatically, use the `skill` tool "
            "with the skill name and optional arguments."
        )
        lines.append("")

    return "\n".join(lines)


def _token_list(text: str) -> list[str]:
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(raw)]
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    expanded: list[str] = []
    for token in tokens:
        if not token.strip() or token in _STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def retrieve_relevant_skills(
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.08,
) -> list[dict[str, Any]]:
    query_terms = _token_list(query)
    query_tokens = set(query_terms)
    if not query_tokens:
        return []

    docs: list[tuple[SkillDefinition, list[str]]] = []
    document_frequency: Counter[str] = Counter()
    for skill in discover_skills():
        meta_terms = _token_list("\n".join([skill.name, skill.description, skill.when_to_use or ""]))
        body_terms = _token_list(skill.prompt_template[:2500])
        terms = (meta_terms * 3) + body_terms
        if not terms:
            continue
        docs.append((skill, terms))
        document_frequency.update(set(terms))
    if not docs:
        return []

    avg_doc_len = sum(len(terms) for _, terms in docs) / max(1, len(docs))
    doc_count = len(docs)
    k1 = 1.4
    b = 0.75
    hits: list[dict[str, Any]] = []
    for skill, terms in docs:
        term_counts = Counter(terms)
        overlap = query_tokens & set(term_counts)
        if not overlap:
            continue
        raw_score = 0.0
        doc_len = max(1, len(terms))
        for token in overlap:
            tf = term_counts[token]
            idf = math.log(
                1 + (doc_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
            )
            denom = tf + k1 * (1 - b + b * doc_len / max(1.0, avg_doc_len))
            raw_score += idf * (tf * (k1 + 1)) / max(denom, 0.0001)
        name_bonus = 0.15 if skill.name.lower() in str(query or "").lower() else 0.0
        score = min(1.0, (raw_score / max(3.0, len(query_tokens))) + name_bonus)
        if score < float(min_score):
            continue
        hits.append(
            {
                "score": float(score),
                "name": skill.name,
                "description": skill.description,
                "when_to_use": skill.when_to_use or "",
                "source": skill.source,
                "context": skill.context,
                "user_invocable": bool(skill.user_invocable),
                "skill_dir": skill.skill_dir,
            }
        )

    hits.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return hits[: max(1, int(limit or 1))]


def format_retrieved_skill_context(query: str, *, limit: int = 3) -> tuple[str, dict[str, Any] | None]:
    hits = retrieve_relevant_skills(query, limit=limit)
    if not hits:
        return "", None
    lines = [
        "<retrieved_skills>",
        "These skills were retrieved for the current user request. "
        "Use a skill only if it directly matches the user's intent; otherwise ignore this block.",
    ]
    for idx, hit in enumerate(hits, start=1):
        lines.append(
            f"{idx}. {hit['name']} (score={float(hit['score']):.3f}, source={hit['source']}): "
            f"{hit['description']}"
        )
        if hit.get("when_to_use"):
            lines.append(f"   When to use: {hit['when_to_use']}")
    lines.append("</retrieved_skills>")
    top = dict(hits[0])
    top["all_hits"] = hits
    return "\n".join(lines), top


def reset_skill_cache() -> None:
    global _cached_skills
    _cached_skills = None
