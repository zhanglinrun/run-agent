"""Dangerous-command approval: detection, per-session pending state, allow-patterns.

Ported from hermes-agent ``tools/approval.py`` (the gateway half). A ``bash`` call whose
command matches a dangerous pattern is held until the chat answers: once, for the
session, always, or deny. ``always`` grants are persisted to ``approvals.json`` under the
gateway state directory; ``session`` grants live for the process. A pending approval that
nobody answers within the timeout is denied, so a turn can never hang forever.

The hardline floor (root / home / system-directory deletion, block-device writes, fork
bombs, shutdown) is never auto-approved by a stored grant: the user answers each time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ApprovalChoice = Literal["once", "session", "always", "deny"]

_SSH_PATH = r"(?:~|\$home|\$\{home\})/\.ssh(?:/|$)"
_SHELL_RC = r"(?:~|\$home|\$\{home\})/\.(?:bashrc|zshrc|profile|bash_profile|zprofile)\b"
_CREDENTIAL_FILES = r"(?:~|\$home|\$\{home\})/\.(?:netrc|pgpass|npmrc|pypirc)\b"
_RUN_ENV = r"(?:~/\.run/|(?:\$home|\$\{home\})/\.run/)\.env\b"
_PROJECT_ENV = r"(?:(?:/|\.{1,2}/)?(?:[^\s/\"'`]+/)*\.env(?:\.[^/\s\"'`]+)*)"
_SYSTEM_CONFIG = r"(?:/etc/|/private/(?:etc|var|tmp|home)/)"
_SENSITIVE_TARGET = (
    rf"(?:{_SYSTEM_CONFIG}|/dev/sd|{_SSH_PATH}|{_RUN_ENV}|{_SHELL_RC}|{_CREDENTIAL_FILES})"
)
_USER_SENSITIVE_TARGET = rf"(?:{_SSH_PATH}|{_SHELL_RC}|{_CREDENTIAL_FILES})"
_TAIL = r"(?:\s*(?:&&|\|\||;).*)?$"
_CMDPOS = (
    r"(?:^|[\n`;|&]|\$\()\s*(?:sudo\s+(?:-[^\s]+\s+)*)?(?:env\s+(?:\w+=\S*\s+)*)?"
    r"(?:(?:exec|nohup|setsid|time)\s+)*"
)
_HARDLINE_DIRS = (
    r"/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*"
    r"|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*"
)
_RM_PREFIX = _CMDPOS + r"rm\s+(-[^\s]*\s+)*"


def _rm_path(alternatives: str) -> str:
    return rf"[\"']?(?:{alternatives})[\"']?(?:\s|$|[)`;|&])"


# Never satisfiable by a stored grant; the user answers every time.
HARDLINE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        _RM_PREFIX + _rm_path(r"/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*"),
        "recursive delete of root filesystem",
    ),
    (_RM_PREFIX + _rm_path(_HARDLINE_DIRS), "recursive delete of system directory"),
    (_RM_PREFIX + _rm_path(r"(?:~|\$\{?HOME\}?)(?:/?|/\*)?"), "recursive delete of home directory"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "format filesystem (mkfs)"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", "dd to raw block device"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "redirect to raw block device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bkill\s+(-[^\s]+\s+)*-1\b", "kill all processes"),
    (_CMDPOS + r"(shutdown|reboot|halt|poweroff)\b", "system shutdown/reboot"),
    (_CMDPOS + r"init\s+[06]\b", "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r"systemctl\s+(poweroff|reboot|halt|kexec)\b", "systemctl poweroff/reboot"),
)

DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    (
        r"\brm\s+(?!--(?:\s|$))(?:(?!\s--(?:\s|$))[^\n\"';|&])*\s(?:-[a-z]*r[a-z]*\b|--recursive\b)",
        "recursive delete (flags after operands)",
    ),
    (
        r"\bcmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b",
        "Windows cmd destructive delete",
    ),
    (
        r"\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+(?:-(?:command|c)\s+)?[\"']?"
        r"(?:remove-item|rmdir|erase|del|rd|ri|rm)\b",
        "Windows PowerShell destructive delete",
    ),
    (
        r"\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:encodedcommand|enc|e)\b",
        "PowerShell encoded command execution",
    ),
    (
        r"\bremove-item\b[^\n;|&]*\s-(?:recurse|force)\b",
        "PowerShell destructive delete (Remove-Item)",
    ),
    (
        r"\b(?:del|erase|rd|rmdir)\s+(?:/[a-z]\s+)*/[sq]\b",
        "Windows destructive delete (recursive/quiet switch)",
    ),
    (
        r"\b(?:iwr|invoke-webrequest|invoke-restmethod|irm|curl|wget)\b[^\n]*\|\s*(?:iex|invoke-expression)\b",
        "pipe remote content to PowerShell (iwr | iex)",
    ),
    (r"\btaskkill\b[^\n]*\s/f\b", "force kill processes (taskkill /F)"),
    (r"\bstop-process\b[^\n]*\s-force\b", "force kill processes (Stop-Process -Force)"),
    (r"\bformat-volume\b", "format filesystem (Format-Volume)"),
    (r"\bclear-disk\b", "wipe disk (Clear-Disk)"),
    (r"\bdiskpart\b", "disk partitioning (diskpart)"),
    (r"\bformat(?:\.com)?\s+[a-z]:", "format drive (format.com)"),
    (r"\bvssadmin\b[^\n]*\bdelete\s+shadows\b", "delete volume shadow copies (vssadmin)"),
    (r"\breg(?:\.exe)?\s+delete\b", "registry delete (reg delete)"),
    (
        r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b",
        "world/other-writable permissions",
    ),
    (
        r"\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)",
        "recursive world/other-writable (long flag)",
    ),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*if=", "disk copy"),
    (r">\s*/dev/sd", "write to block device"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    (r"\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)", "SQL DELETE without WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    (rf">\s*{_SYSTEM_CONFIG}", "overwrite system config"),
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b", "stop/restart system service"),
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
    (r"\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b", "force kill processes (killall -KILL)"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)", "pipe remote content to shell"),
    (
        r"\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b",
        "execute remote script via process substitution",
    ),
    (
        r"(?:\beval\b|\bsource\b|\.)\s*(?:\$\(\s*|`\s*)(?:curl|wget)\b",
        "execute remote content via command substitution",
    ),
    (
        r"\b(base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b",
        "pipe decoded content to shell (possible command obfuscation)",
    ),
    (rf"\btee\b.*[\"']?{_SENSITIVE_TARGET}", "overwrite system file via tee"),
    (rf">>?\s*[\"']?{_SENSITIVE_TARGET}", "overwrite system file via redirection"),
    (
        rf">>?\s*[\"']?{_PROJECT_ENV}[\"']?(?=[\s;&|<>\"']|$)",
        "overwrite project env via redirection",
    ),
    (r"\bxargs\s+.*\brm\b", "xargs with rm"),
    (r"\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b", "find -exec/-execdir rm"),
    (r"\bfind\b.*-delete\b", "find -delete"),
    (
        r"\bdocker(?:-compose|\s+compose)\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill|down)\b",
        "docker compose restart/stop/kill/down (container lifecycle)",
    ),
    (
        r"\bdocker\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*(restart|stop|kill)\b",
        "docker restart/stop/kill (container lifecycle)",
    ),
    (
        r"\b(pkill|killall)\b.*\b(run-agent|run_agent|gateway)\b",
        "kill gateway process (self-termination)",
    ),
    (
        r"\bkill\b.*\$\(\s*(pgrep|pidof)\b",
        "kill process via pgrep/pidof expansion (self-termination)",
    ),
    (
        rf"\b(cp|mv|install)\b.*\s[\"']?{_SENSITIVE_TARGET}[^\s\"']*[\"']?{_TAIL}",
        "copy/move file into sensitive credential/SSH/shell-rc path",
    ),
    (
        rf"\bsed\s+-[^\s]*i.*(?:{_USER_SENSITIVE_TARGET})[^\s\"']*",
        "in-place edit of sensitive credential/SSH/shell-rc path",
    ),
    (rf"\bsed\s+-[^\s]*i.*\s{_SYSTEM_CONFIG}", "in-place edit of system config"),
    (rf"\bsed\s+-[^\s]*i.*{_RUN_ENV}", "in-place edit of the gateway .env"),
    (r"\b(bash|sh|zsh|ksh)\s+<<", "shell execution via heredoc"),
    (r"\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b", "git reset --hard (destroys uncommitted changes)"),
    (r"\bgit\s+push\b.*--forc[a-z]*\b", "git force push (rewrites remote history)"),
    (r"\bgit\s+push\b.*-f\b", "git force push short flag (rewrites remote history)"),
    (r"\bgit\s+clean\s+-[^\s]*f", "git clean with force (deletes untracked files)"),
    (r"\bgit\s+branch\s+-D\b", "git branch force delete"),
    (r"\bchmod\s+\+x\b.*[;&|]+\s*\./", "chmod +x followed by immediate execution"),
    (r"\bsudo\b[^;|&\n]*?\s-[a-z]*[sa][a-z]*\b", "sudo with stdin/askpass/shell flag"),
    (r"\bcrontab\s+(-[^\s]*\s+)*-r\b", "remove all cron jobs"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", "system shutdown/reboot"),
)

_FLAGS = re.IGNORECASE | re.DOTALL
_HARDLINE_COMPILED = tuple((re.compile(p, _FLAGS), d) for p, d in HARDLINE_PATTERNS)
_DANGEROUS_COMPILED = tuple((re.compile(p, _FLAGS), d) for p, d in DANGEROUS_PATTERNS)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def normalize_command(command: str) -> str:
    """Strip obfuscation before matching: ANSI, NULs, NFKC folding, line continuations."""
    text = _ANSI_RE.sub("", command).replace("\x00", "")
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\\\r?\n", "", text)
    home = os.path.expanduser("~")
    if home and home not in {"~", "/"}:
        text = text.replace(home, "~")
        text = text.replace(home.replace("\\", "/"), "~")
    return text


_SEARCH_TOOLS = ("grep", "rg", "egrep", "fgrep", "ag", "ack")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _hide_search_operands(command: str) -> str:
    """Blank quoted operands of a search command so ``grep 'rm -rf' src`` is not flagged."""
    head = command.lstrip().split(maxsplit=1)
    if not head or head[0].rsplit("/", 1)[-1] not in _SEARCH_TOOLS:
        return command
    return _QUOTED_RE.sub("''", command)


def _variants(command: str) -> list[str]:
    normalized = normalize_command(_hide_search_operands(command))
    variants = [normalized]
    if re.search(r"[A-Za-z]:\\", command) or command.startswith("\\\\"):
        variants.append(normalize_command(command.replace("\\", "/")))
    return variants


@dataclass(frozen=True, slots=True)
class DangerVerdict:
    dangerous: bool
    description: str | None = None
    hardline: bool = False

    @property
    def pattern_key(self) -> str | None:
        return self.description


def detect_dangerous_command(command: str) -> DangerVerdict:
    """Whether ``command`` needs approval, why, and whether a stored grant may cover it."""
    for variant in _variants(command):
        lowered = variant.lower()
        for pattern, description in _HARDLINE_COMPILED:
            if pattern.search(lowered):
                return DangerVerdict(True, description, hardline=True)
        for pattern, description in _DANGEROUS_COMPILED:
            if pattern.search(lowered):
                return DangerVerdict(True, description)
    return DangerVerdict(False)


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    session_key: str
    command: str
    description: str
    hardline: bool
    created_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    choice: ApprovalChoice | None = None
    reason: str | None = None
    card_message_id: str | None = None
    resolved_by: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    chat_type: str | None = None


class ApprovalStore:
    """Grants that outlive one prompt: session-scoped in memory, permanent on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._session: dict[str, set[str]] = {}
        self._permanent: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        keys = data.get("always", []) if isinstance(data, dict) else []
        self._permanent = {str(k) for k in keys if isinstance(k, str)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "always": sorted(self._permanent)}, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def is_allowed(self, session_key: str, verdict: DangerVerdict) -> bool:
        if verdict.hardline or verdict.pattern_key is None:
            return False
        key = verdict.pattern_key
        return key in self._permanent or key in self._session.get(session_key, set())

    def grant(self, session_key: str, verdict: DangerVerdict, choice: ApprovalChoice) -> None:
        key = verdict.pattern_key
        if key is None or verdict.hardline:
            return
        if choice == "session":
            self._session.setdefault(session_key, set()).add(key)
        elif choice == "always":
            self._permanent.add(key)
            self._save()

    def clear_session(self, session_key: str) -> None:
        self._session.pop(session_key, None)

    @property
    def permanent(self) -> frozenset[str]:
        return frozenset(self._permanent)


class ApprovalRegistry:
    """Per-session FIFO of pending approvals, answered by the chat or by timeout."""

    def __init__(self, store: ApprovalStore, *, timeout_seconds: float = 300.0) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self._queues: dict[str, list[PendingApproval]] = {}
        self._by_id: dict[str, PendingApproval] = {}

    def has_blocking(self, session_key: str) -> bool:
        return bool(self._queues.get(session_key))

    def pending(self, session_key: str) -> tuple[PendingApproval, ...]:
        return tuple(self._queues.get(session_key, ()))

    def get(self, approval_id: str) -> PendingApproval | None:
        return self._by_id.get(approval_id)

    def create(
        self,
        session_key: str,
        command: str,
        verdict: DangerVerdict,
        *,
        chat_id: str | None = None,
        thread_id: str | None = None,
        chat_type: str | None = None,
    ) -> PendingApproval:
        entry = PendingApproval(
            approval_id=f"ap{secrets.token_urlsafe(18)}",
            session_key=session_key,
            command=command,
            description=verdict.description or "dangerous command",
            hardline=verdict.hardline,
            created_at=time.time(),
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type=chat_type,
        )
        self._queues.setdefault(session_key, []).append(entry)
        self._by_id[entry.approval_id] = entry
        return entry

    async def wait(self, entry: PendingApproval) -> ApprovalChoice:
        """Block until answered; an unanswered approval is denied at the timeout."""
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await entry.event.wait()
        except TimeoutError:
            self._resolve_entry(entry, "deny", reason="approval timed out", resolved_by="timeout")
        return entry.choice or "deny"

    def resolve(
        self,
        session_key: str,
        choice: ApprovalChoice,
        *,
        resolve_all: bool = False,
        reason: str | None = None,
        approval_id: str | None = None,
        resolved_by: str | None = None,
    ) -> int:
        """Answer the oldest pending approval (or all, or one by id); returns how many."""
        queue = self._queues.get(session_key)
        if not queue:
            return 0
        if approval_id is not None:
            targets = [e for e in queue if e.approval_id == approval_id]
        elif resolve_all:
            targets = list(queue)
        else:
            targets = [queue[0]]
        for entry in targets:
            self._resolve_entry(entry, choice, reason=reason, resolved_by=resolved_by)
        return len(targets)

    def resolve_by_id(
        self, approval_id: str, choice: ApprovalChoice, *, resolved_by: str | None = None
    ) -> PendingApproval | None:
        entry = self._by_id.get(approval_id)
        if entry is None or entry.choice is not None:
            return None
        self._resolve_entry(entry, choice, resolved_by=resolved_by)
        return entry

    def _resolve_entry(
        self,
        entry: PendingApproval,
        choice: ApprovalChoice,
        *,
        reason: str | None = None,
        resolved_by: str | None = None,
    ) -> None:
        if entry.choice is None:
            entry.choice = choice
            entry.reason = reason
            entry.resolved_by = resolved_by
            if choice in {"session", "always"}:
                self.store.grant(
                    entry.session_key,
                    DangerVerdict(True, entry.description, entry.hardline),
                    choice,
                )
        queue = self._queues.get(entry.session_key)
        if queue and entry in queue:
            queue.remove(entry)
            if not queue:
                self._queues.pop(entry.session_key, None)
        self._by_id.pop(entry.approval_id, None)
        entry.event.set()

    def cancel_session(self, session_key: str, reason: str = "cancelled") -> int:
        return self.resolve(
            session_key, "deny", resolve_all=True, reason=reason, resolved_by="gateway"
        )


# -- text fallbacks ----------------------------------------------------------------------

APPROVE_WORDS = frozenset({"approve", "yes", "ok", "okay", "confirm", "y", "👍"})
DENY_WORDS = frozenset({"deny", "no", "reject", "cancel", "n", "👎"})
ALWAYS_WORDS = frozenset({"always", "approve always", "always approve"})
SESSION_WORDS = frozenset({"session", "approve session", "session approve"})


def route_approval_reply(text: str) -> str | None:
    """Turn a bare-word answer into the canonical ``/approve`` or ``/deny`` text."""
    raw = text.strip().lower()
    if raw in APPROVE_WORDS:
        return "/approve"
    if raw in DENY_WORDS:
        return "/deny"
    if raw in ALWAYS_WORDS:
        return "/approve always"
    if raw in SESSION_WORDS:
        return "/approve session"
    return None


def format_exec_approval_text(
    command: str,
    description: str,
    *,
    allow_session: bool = True,
    allow_always: bool = True,
) -> str:
    """The text prompt shown when the platform cannot render buttons."""
    preview = command[:200] + "..." if len(command) > 200 else command
    choices = ["Reply `/approve` to execute this one operation"]
    if allow_session:
        choices.append("`/approve session` to approve this pattern for the session")
        if allow_always:
            choices.append("`/approve always` to approve permanently")
    choices.append("`/deny` to cancel")
    return (
        f"⚠️ **Dangerous command requires approval:**\n```\n{preview}\n```\n"
        f"Reason: {description}\n\n" + ", ".join(choices[:-1]) + f", or {choices[-1]}."
    )


ApprovalPrompter = Callable[[PendingApproval], "asyncio.Future[None] | None"]


__all__ = [
    "ALWAYS_WORDS",
    "APPROVE_WORDS",
    "DANGEROUS_PATTERNS",
    "DENY_WORDS",
    "HARDLINE_PATTERNS",
    "SESSION_WORDS",
    "ApprovalChoice",
    "ApprovalRegistry",
    "ApprovalStore",
    "DangerVerdict",
    "PendingApproval",
    "detect_dangerous_command",
    "format_exec_approval_text",
    "normalize_command",
    "route_approval_reply",
]
