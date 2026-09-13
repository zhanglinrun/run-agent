"""Shared threat patterns for content that enters the system prompt.

Memory entries and Skills are pasted into the prompt of every later session, so a
poisoned entry persists until someone removes it. This is the single pattern library
used by the memory store (writes and the load-time snapshot) and the skill guard.

Patterns are organised by attack class and carry a scope:

- ``all``: classic prompt injection and secret exfiltration; safe on any text.
- ``context``: adds role hijack and command-and-control vocabulary; for memory,
  context files and tool results.
- ``strict``: adds persistence, backdoor and hardcoded-secret checks; for writes a user
  can resolve interactively (memory tool, skill installs).

Patterns anchor on attack-specific vocabulary rather than on bossy English, and use a
bounded filler between key tokens so a few inserted words do not bypass them while the
regex still cannot backtrack without bound.
"""

from __future__ import annotations

import re
import unicodedata

MAX_SCAN_CHARS = 65_536
_FILLER = r"(?:\w+\s+){0,8}"

_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # classic prompt injection
    (
        rf"ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions",
        "prompt_injection",
        "all",
    ),
    (r"system\s+prompt\s+override", "sys_prompt_override", "all"),
    (
        rf"disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)",
        "disregard_rules",
        "all",
    ),
    (
        r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->",
        "html_comment_injection",
        "all",
    ),
    (
        rf"act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don't\s+have)\s+{_FILLER}(restrictions|limits|rules)",
        "bypass_restrictions",
        "all",
    ),
    (r"<\s*div\s+style\s*=\s*[\"'][^>]{0,2048}display\s*:\s*none", "hidden_div", "all"),
    (
        r"translate\s+[^\n]{0,512}\s+into\s+[^\n]{0,512}\s+and\s+(execute|run|eval)",
        "translate_execute",
        "all",
    ),
    (rf"do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user", "deception_hide", "all"),
    (r"忽略(?:之前|上面|所有|以上)的?(?:指令|指示|规则)", "prompt_injection_zh", "all"),
    (r"不要(?:告诉|让)用户(?:知道)?", "deception_hide_zh", "all"),
    # role-play / identity hijack
    (rf"you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+", "role_hijack", "context"),
    (rf"pretend\s+{_FILLER}(you\s+are|to\s+be)\s+", "role_pretend", "context"),
    (rf"output\s+{_FILLER}(system|initial)\s+prompt", "leak_system_prompt", "context"),
    (
        rf"(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)",
        "remove_filters",
        "context",
    ),
    (
        rf"you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to",
        "fake_update",
        "context",
    ),
    (r"\bname\s+yourself\s+\w+", "identity_override", "context"),
    # command-and-control promptware
    (r"register\s+(as\s+)?a?\s*node", "c2_node_registration", "context"),
    (r"(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+", "c2_heartbeat", "context"),
    (r"pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b", "c2_task_pull", "context"),
    (r"connect\s+to\s+the\s+network\b", "c2_network_connect", "context"),
    (r"you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b", "forced_action", "context"),
    (r"only\s+use\s+one[\s\-]?liners?\b", "anti_forensic_oneliner", "context"),
    (
        rf"never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk",
        "anti_forensic_disk",
        "context",
    ),
    (
        r"unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC|RUN_AGENT)\w*",
        "env_var_unset_agent",
        "context",
    ),
    (
        r"\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b",
        "known_c2_framework",
        "context",
    ),
    (r"\bc2\s+(?:server|channel|infrastructure|beacon)\b", "c2_explicit", "context"),
    (r"\bcommand\s+and\s+control\b", "c2_explicit_long", "context"),
    # exfiltration
    (
        r"curl\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
        "exfil_curl",
        "all",
    ),
    (
        r"wget\s+[^\n]{0,2048}\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)",
        "exfil_wget",
        "all",
    ),
    (
        r"cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)",
        "read_secrets",
        "all",
    ),
    (r"(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://", "send_to_url", "strict"),
    (
        rf"(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)",
        "context_exfil",
        "strict",
    ),
    # persistence / backdoors
    (r"authorized_keys", "ssh_backdoor", "strict"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access", "strict"),
    (r"\$HOME/\.run/\.env|~/\.run/\.env|~/\.run/settings\.json", "run_agent_env", "strict"),
    (
        r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)",
        "agent_config_mod",
        "strict",
    ),
    # hardcoded secrets
    (
        r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}",
        "hardcoded_secret",
        "strict",
    ),
)

INVISIBLE_CHARS = frozenset(
    {
        "​",
        "‌",
        "‍",
        "⁠",
        "⁢",
        "⁣",
        "⁤",
        "﻿",
        "‪",
        "‫",
        "‬",
        "‭",
        "‮",
        "⁦",
        "⁧",
        "⁨",
        "⁩",
    }
)

_COMPILED: dict[str, list[tuple[re.Pattern[str], str]]] = {"all": [], "context": [], "strict": []}
for _pattern, _id, _scope in _PATTERNS:
    _entry = (re.compile(_pattern, re.IGNORECASE), _id)
    if _scope == "all":
        _COMPILED["all"].append(_entry)
        _COMPILED["context"].append(_entry)
        _COMPILED["strict"].append(_entry)
    elif _scope == "context":
        _COMPILED["context"].append(_entry)
        _COMPILED["strict"].append(_entry)
    else:
        _COMPILED["strict"].append(_entry)


def scan_for_threats(content: str, scope: str = "context") -> list[str]:
    """Return the matched pattern IDs in ``content`` at the given scope.

    Invisible and bidirectional Unicode characters are reported as
    ``invisible_unicode_U+XXXX`` from the raw text; the regex pass runs on the NFKC
    normalisation so full-width or compatibility variants cannot dodge a keyword.
    """
    if not content:
        return []
    patterns = _COMPILED.get(scope)
    if patterns is None:
        raise ValueError(f"unknown threat scan scope {scope!r}")
    text = content[:MAX_SCAN_CHARS]
    findings = [f"invisible_unicode_U+{ord(ch):04X}" for ch in sorted(set(text) & INVISIBLE_CHARS)]
    normalised = unicodedata.normalize("NFKC", text)
    findings.extend(pid for compiled, pid in patterns if compiled.search(normalised))
    return findings


def first_threat_message(content: str, scope: str = "strict") -> str | None:
    """A human-readable refusal for the first threat found, or None when clean."""
    findings = scan_for_threats(content, scope)
    if not findings:
        return None
    return (
        f"Content blocked: it matches threat pattern(s) {', '.join(findings)}. "
        "Rewrite it without instructions aimed at the assistant, secrets, or hidden "
        "characters, then retry."
    )


__all__ = ["INVISIBLE_CHARS", "MAX_SCAN_CHARS", "first_threat_message", "scan_for_threats"]
