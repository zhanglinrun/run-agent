"""DM pairing: code-based approval for users who are not on the allowlist.

Ported from hermes-agent ``gateway/pairing.py``. Instead of only a static allowlist,
an unknown user receives a one-time pairing code that the bot owner approves from the
command line; the grant is then honoured by the gateway's authorization check.

Security properties (OWASP / NIST SP 800-63-4):
  - 8-character codes from a 32-character unambiguous alphabet (no 0/O, 1/I)
  - cryptographic randomness via ``secrets.choice``
  - codes stored only as salted SHA-256 hashes; never logged
  - 1-hour code expiry; max 3 pending codes per platform
  - rate limit: 1 request per user per 10 minutes
  - lockout for 1 hour after 5 failed approval attempts
  - data files written with mode 0600
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 3600
RATE_LIMIT_SECONDS = 600
LOCKOUT_SECONDS = 3600
MAX_PENDING_PER_PLATFORM = 3
MAX_FAILED_ATTEMPTS = 5


def _secure_write(path: Path, data: str) -> None:
    """Write atomically and restrict the file to the owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".pairing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class PairingStore:
    """Pending codes and approved users, one JSON file each per platform."""

    def __init__(self, directory: Path, *, clock: Any = time.time) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._clock = clock

    # -- files --------------------------------------------------------------------

    def _pending_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-pending.json"

    def _approved_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-approved.json"

    def _rate_limit_path(self) -> Path:
        return self._dir / "_rate_limits.json"

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _save(path: Path, data: dict[str, Any]) -> None:
        _secure_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    # -- approved users -------------------------------------------------------------

    def is_approved(self, platform: str, user_id: str | None) -> bool:
        if not user_id:
            return False
        return user_id in self._load(self._approved_path(platform))

    def list_approved(self, platform: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        platforms = [platform] if platform else self._all_platforms("approved")
        for name in platforms:
            for uid, info in self._load(self._approved_path(name)).items():
                results.append({"platform": name, "user_id": uid, **info})
        return results

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        path = self._approved_path(platform)
        approved = self._load(path)
        approved[user_id] = {"user_name": user_name, "approved_at": self._clock()}
        self._save(path, approved)

    def revoke(self, platform: str, user_id: str) -> bool:
        path = self._approved_path(platform)
        with self._lock:
            approved = self._load(path)
            if user_id not in approved:
                return False
            del approved[user_id]
            self._save(path, approved)
            return True

    # -- pending codes --------------------------------------------------------------

    @staticmethod
    def _hash_code(code: str, salt: bytes) -> str:
        return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()

    def generate_code(self, platform: str, user_id: str, user_name: str = "") -> str | None:
        """A fresh pairing code, or ``None`` when rate-limited, locked out or full."""
        with self._lock:
            self._cleanup_expired(platform)
            if self._is_locked_out(platform) or self._is_rate_limited(platform, user_id):
                return None
            pending = self._load(self._pending_path(platform))
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            salt = os.urandom(16)
            pending[secrets.token_hex(8)] = {
                "hash": self._hash_code(code, salt),
                "salt": salt.hex(),
                "user_id": user_id,
                "user_name": user_name,
                "created_at": self._clock(),
            }
            self._save(self._pending_path(platform), pending)
            self._record_rate_limit(platform, user_id)
            return code

    def approve_code(self, platform: str, code: str) -> dict[str, str] | None:
        """Approve a code; ``{user_id, user_name}`` on success, ``None`` otherwise."""
        with self._lock:
            self._cleanup_expired(platform)
            code = code.upper().strip()
            if self._is_locked_out(platform):
                return None
            pending = self._load(self._pending_path(platform))
            for key, entry in pending.items():
                if not isinstance(entry, dict) or "hash" not in entry or "salt" not in entry:
                    continue
                try:
                    salt = bytes.fromhex(str(entry["salt"]))
                except ValueError:
                    continue
                if hmac.compare_digest(self._hash_code(code, salt), str(entry["hash"])):
                    del pending[key]
                    self._save(self._pending_path(platform), pending)
                    self._reset_failed_attempts(platform)
                    self._approve_user(
                        platform, str(entry["user_id"]), str(entry.get("user_name", ""))
                    )
                    return {
                        "user_id": str(entry["user_id"]),
                        "user_name": str(entry.get("user_name", "")),
                    }
            self._record_failed_attempt(platform)
            return None

    def list_pending(self, platform: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        platforms = [platform] if platform else self._all_platforms("pending")
        with self._lock:
            for name in platforms:
                self._cleanup_expired(name)
                for entry in self._load(self._pending_path(name)).values():
                    if isinstance(entry, dict):
                        results.append(
                            {
                                "platform": name,
                                "user_id": entry.get("user_id"),
                                "user_name": entry.get("user_name", ""),
                                "created_at": entry.get("created_at"),
                                "expires_at": float(entry.get("created_at", 0)) + CODE_TTL_SECONDS,
                            }
                        )
        return results

    def has_pending_for(self, platform: str, user_id: str) -> bool:
        with self._lock:
            self._cleanup_expired(platform)
            return any(
                isinstance(e, dict) and e.get("user_id") == user_id
                for e in self._load(self._pending_path(platform)).values()
            )

    # -- limits ---------------------------------------------------------------------

    def _cleanup_expired(self, platform: str) -> None:
        path = self._pending_path(platform)
        pending = self._load(path)
        now = self._clock()
        kept = {
            k: v
            for k, v in pending.items()
            if isinstance(v, dict) and now - float(v.get("created_at", 0)) < CODE_TTL_SECONDS
        }
        if len(kept) != len(pending):
            self._save(path, kept)

    def _rate_limits(self) -> dict[str, Any]:
        return self._load(self._rate_limit_path())

    def _is_rate_limited(self, platform: str, user_id: str) -> bool:
        last = self._rate_limits().get("requests", {}).get(f"{platform}:{user_id}")
        return last is not None and self._clock() - float(last) < RATE_LIMIT_SECONDS

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        data = self._rate_limits()
        requests = data.setdefault("requests", {})
        requests[f"{platform}:{user_id}"] = self._clock()
        cutoff = self._clock() - RATE_LIMIT_SECONDS
        data["requests"] = {k: v for k, v in requests.items() if float(v) >= cutoff}
        self._save(self._rate_limit_path(), data)

    def _is_locked_out(self, platform: str) -> bool:
        info = self._rate_limits().get("lockouts", {}).get(platform)
        if not isinstance(info, dict):
            return False
        until = float(info.get("until", 0))
        return bool(until > float(self._clock()))

    def _record_failed_attempt(self, platform: str) -> None:
        data = self._rate_limits()
        failures = data.setdefault("failures", {})
        count = int(failures.get(platform, 0)) + 1
        failures[platform] = count
        if count >= MAX_FAILED_ATTEMPTS:
            data.setdefault("lockouts", {})[platform] = {"until": self._clock() + LOCKOUT_SECONDS}
            failures[platform] = 0
        self._save(self._rate_limit_path(), data)

    def _reset_failed_attempts(self, platform: str) -> None:
        data = self._rate_limits()
        if data.get("failures", {}).pop(platform, None) is not None:
            self._save(self._rate_limit_path(), data)

    def _all_platforms(self, kind: str) -> list[str]:
        return sorted(
            p.name[: -len(f"-{kind}.json")]
            for p in self._dir.glob(f"*-{kind}.json")
            if not p.name.startswith("_")
        )


def format_pairing_message(code: str) -> str:
    """What an unknown user is told; the owner approves from the CLI."""
    return (
        "👋 Hi! This bot is private, so a bot owner has to approve you before we can talk.\n\n"
        f"Your pairing code is: `{code}`\n\n"
        f"Ask the bot owner to run `run gateway pairing approve feishu {code}`. "
        "The code expires in 1 hour."
    )


__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "LOCKOUT_SECONDS",
    "MAX_FAILED_ATTEMPTS",
    "MAX_PENDING_PER_PLATFORM",
    "RATE_LIMIT_SECONDS",
    "PairingStore",
    "format_pairing_message",
]
