"""Gateway configuration read from the environment.

The gateway serves one channel, Feishu. Everything it needs comes from environment
variables (a project ``.env`` is loaded by the command line first), following the same
approach as the model providers: no catalog, no identity map, no extra config file.

The knobs and their defaults follow hermes-agent's gateway (``gateway/config.py``,
``gateway/run.py`` and the Feishu adapter plugin) so the two behave the same way out of
the box; only the spelling of the variables is ours.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

ResetMode = Literal["none", "idle", "daily", "both"]
ReplyToMode = Literal["off", "first", "all"]
ConnectionMode = Literal["websocket", "webhook"]
GroupPolicy = Literal["open", "allowlist", "blocklist", "admin_only", "disabled"]
AllowBots = Literal["none", "mentions", "all"]
UnauthorizedDmBehavior = Literal["pair", "ignore", "reply"]
BusyInputMode = Literal["interrupt", "queue", "steer"]
TableMode = Literal["table", "bullets", "code", "off"]
ToolProgressMode = Literal["off", "new", "all", "verbose"]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"Expected a boolean, got {value!r}")


def _int(value: str | None, default: int, *, minimum: int) -> int:
    if value is None or not value.strip():
        return default
    number = int(value)
    if number < minimum:
        raise ValueError(f"Expected an integer >= {minimum}, got {value!r}")
    return number


def _float(value: str | None, default: float, *, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if number < minimum:
        raise ValueError(f"Expected a number >= {minimum}, got {value!r}")
    return number


def _csv(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _choice(value: str | None, default: str, allowed: tuple[str, ...], name: str) -> str:
    text = (value or default).strip().lower()
    if text not in allowed:
        raise ValueError(f"Unknown {name}: {value!r} (expected one of {', '.join(allowed)})")
    return text


@dataclass(frozen=True, slots=True)
class SessionResetPolicy:
    """When a chat loses its conversation context.

    ``none`` keeps every chat on one session until ``/new``. ``idle`` starts a fresh
    session after ``idle_minutes`` without activity, ``daily`` at ``at_hour`` local time,
    and ``both`` whichever comes first. ``notify`` tells the user when that happened,
    but only when the expired session had seen a message.
    """

    mode: ResetMode = "none"
    at_hour: int = 4
    idle_minutes: int = 1440
    notify: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"none", "idle", "daily", "both"}:
            raise ValueError(f"Unknown session reset mode: {self.mode}")
        if not 0 <= self.at_hour <= 23:
            raise ValueError("Session reset hour must be between 0 and 23")
        if self.idle_minutes < 1:
            raise ValueError("Session idle minutes must be positive")


@dataclass(frozen=True, slots=True)
class GroupRule:
    """Per-group override of the group policy (hermes ``feishu.group_rules``)."""

    policy: GroupPolicy | None = None
    allowlist: frozenset[str] = frozenset()
    blocklist: frozenset[str] = frozenset()
    require_mention: bool | None = None


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    domain: str | None = None
    # The adapter supports websocket only and explicitly rejects webhook mode.
    connection_mode: ConnectionMode = "websocket"
    # Who may talk to the bot.
    allowed_users: frozenset[str] = frozenset()
    allow_all_users: bool = False
    admins: frozenset[str] = frozenset()
    group_policy: GroupPolicy = "allowlist"
    group_rules: Mapping[str, GroupRule] = field(default_factory=dict)
    allow_bots: AllowBots = "none"
    require_mention: bool = True
    respond_to_mention_all: bool = True
    # How replies go out.
    reply_to_mode: ReplyToMode = "first"
    max_message_length: int = 8000
    table_mode: TableMode = "table"
    streaming: bool = False
    tool_progress: ToolProgressMode = "off"
    # Inbound media handed to the model.
    media_enabled: bool = False
    # Duplicate suppression that survives a restart.
    dedup_cache_size: int = 2048
    dedup_ttl_seconds: float = 24 * 3600.0
    # Approval cards.
    approval_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        if self.connection_mode not in {"websocket", "webhook"}:
            raise ValueError(f"Unknown connection mode: {self.connection_mode}")
        if self.reply_to_mode not in {"off", "first", "all"}:
            raise ValueError(f"Unknown reply mode: {self.reply_to_mode}")
        if self.max_message_length < 200:
            raise ValueError("Feishu message length limit must be at least 200 characters")
        if self.group_policy not in {"open", "allowlist", "blocklist", "admin_only", "disabled"}:
            raise ValueError(f"Unknown group policy: {self.group_policy}")
        if self.allow_bots not in {"none", "mentions", "all"}:
            raise ValueError(f"Unknown allow_bots mode: {self.allow_bots}")
        if self.dedup_cache_size < 1 or self.dedup_ttl_seconds < 0:
            raise ValueError("Dedup cache size must be positive and the TTL non-negative")
        if self.approval_timeout_seconds <= 0:
            raise ValueError("Approval timeout must be positive")

    @property
    def has_allowlist(self) -> bool:
        """Whether the operator restricted access; unknown senders are then ignored."""
        return bool(self.allowed_users) or bool(self.admins)

    def is_admin(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.admins

    def is_authorized(self, user_id: str | None) -> bool:
        """Whether a direct-message sender may use the bot (pairing grants aside)."""
        if self.allow_all_users:
            return True
        return bool(user_id) and (user_id in self.allowed_users or user_id in self.admins)

    def group_rule(self, chat_id: str | None) -> GroupRule | None:
        return self.group_rules.get(chat_id or "")

    def require_mention_for(self, chat_id: str | None) -> bool:
        rule = self.group_rule(chat_id)
        if rule is not None and rule.require_mention is not None:
            return rule.require_mention
        return self.require_mention

    def allows_group_message(
        self, user_id: str | None, chat_id: str | None, *, is_bot: bool = False
    ) -> bool:
        """Per-group policy gate for non-DM traffic (hermes ``_allow_group_message``)."""
        if self.is_admin(user_id):
            return True
        rule = self.group_rule(chat_id)
        if rule is not None and rule.policy is not None:
            policy: GroupPolicy = rule.policy
            allowlist = rule.allowlist or self.allowed_users
            blocklist = rule.blocklist
        else:
            policy = self.group_policy
            allowlist = (
                rule.allowlist if rule is not None and rule.allowlist else None
            ) or self.allowed_users
            blocklist = rule.blocklist if rule is not None else frozenset()
        if policy == "disabled":
            return False
        if policy == "open":
            return True
        if policy == "admin_only":
            return False
        if is_bot:
            return True
        if policy == "allowlist":
            return bool(user_id) and (user_id in allowlist or self.allow_all_users)
        return bool(user_id) and user_id not in blocklist


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    feishu: FeishuConfig
    reset_policy: SessionResetPolicy = SessionResetPolicy()
    group_sessions_per_user: bool = True
    thread_sessions_per_user: bool = False
    # What happens to a DM from someone not on the allowlist: ``pair`` hands them a
    # pairing code, ``ignore`` drops the message silently, ``reply`` tells them.
    # ``None`` picks hermes' default: pair on an open gateway, ignore once an allowlist
    # exists.
    unauthorized_dm_behavior: UnauthorizedDmBehavior | None = None
    # Slash-command access: admins may run every command; other allowed users only the
    # listed ones. An empty admin list switches the gate off (everyone runs everything).
    admin_users: frozenset[str] = frozenset()
    user_allowed_commands: frozenset[str] = frozenset()
    # What a message does while the chat's turn is still running.
    busy_input_mode: BusyInputMode = "interrupt"
    busy_queue_max_pending: int = 32
    # Agent cache and turn lifetime.
    agent_idle_seconds: float = 3600.0
    max_cached_agents: int = 128
    agent_cache_memory_high_mb: int | None = None
    turn_lease_timeout_seconds: float = 1800.0
    agent_timeout_seconds: float = 1800.0
    agent_timeout_warning_seconds: float = 900.0
    stall_timeout_seconds: float = 300.0
    # Shutdown and restart.
    restart_drain_timeout_seconds: float = 0.0
    auto_continue_freshness_seconds: float = 3600.0
    restart_loop_max_boots: int = 3
    restart_loop_window_seconds: float = 60.0
    session_expiry_watch_seconds: float = 300.0
    # Heartbeats.
    heartbeat_poll_seconds: float = 5.0
    heartbeat_min_interval_seconds: float = 60.0
    # Delivery.
    delivery_ledger_enabled: bool = True
    memory_monitor_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.agent_idle_seconds <= 0:
            raise ValueError("Agent idle seconds must be positive")
        if self.max_cached_agents < 1:
            raise ValueError("At least one cached agent is required")
        if self.turn_lease_timeout_seconds <= 0 or self.stall_timeout_seconds < 0:
            raise ValueError("Lease timeout must be positive and the stall timeout non-negative")
        if self.heartbeat_poll_seconds <= 0 or self.heartbeat_min_interval_seconds <= 0:
            raise ValueError("Heartbeat intervals must be positive")
        if self.busy_input_mode not in {"interrupt", "queue", "steer"}:
            raise ValueError(f"Unknown busy input mode: {self.busy_input_mode}")
        if self.busy_queue_max_pending < 1:
            raise ValueError("Busy queue cap must be positive")
        if self.unauthorized_dm_behavior not in {None, "pair", "ignore", "reply"}:
            raise ValueError(f"Unknown unauthorized DM behavior: {self.unauthorized_dm_behavior}")
        if self.agent_timeout_seconds < 0 or self.agent_timeout_warning_seconds < 0:
            raise ValueError("Agent timeouts must be non-negative")
        if self.restart_drain_timeout_seconds < 0 or self.auto_continue_freshness_seconds < 0:
            raise ValueError("Drain timeout and freshness window must be non-negative")

    @property
    def effective_unauthorized_dm_behavior(self) -> UnauthorizedDmBehavior:
        if self.unauthorized_dm_behavior is not None:
            return self.unauthorized_dm_behavior
        return "reply"

    @property
    def slash_access_enabled(self) -> bool:
        return bool(self.admin_users)


def load_gateway_config(env: Mapping[str, str] | None = None) -> GatewayConfig:
    """Build the gateway configuration from environment variables."""
    source = os.environ if env is None else env
    feishu = FeishuConfig(
        app_id=source.get("FEISHU_APP_ID", "").strip(),
        app_secret=source.get("FEISHU_APP_SECRET", "").strip(),
        domain=source.get("FEISHU_DOMAIN", "").strip() or None,
        connection_mode=_connection_mode(source.get("FEISHU_CONNECTION_MODE")),
        allowed_users=_csv(source.get("FEISHU_ALLOWED_USERS")),
        allow_all_users=_bool(source.get("FEISHU_ALLOW_ALL_USERS"), False),
        admins=_csv(source.get("FEISHU_ADMINS")),
        group_policy=_group_policy(source.get("FEISHU_GROUP_POLICY")),
        group_rules=_group_rules(source.get("FEISHU_GROUP_RULES")),
        allow_bots=_allow_bots(source.get("FEISHU_ALLOW_BOTS")),
        require_mention=_bool(source.get("FEISHU_REQUIRE_MENTION"), True),
        respond_to_mention_all=_bool(source.get("FEISHU_RESPOND_TO_MENTION_ALL"), True),
        reply_to_mode=_reply_mode(source.get("FEISHU_REPLY_TO_MODE")),
        max_message_length=_int(source.get("FEISHU_MAX_MESSAGE_LENGTH"), 8000, minimum=200),
        table_mode=_table_mode(source.get("FEISHU_TABLE_MODE")),
        streaming=_bool(source.get("FEISHU_STREAMING"), False),
        tool_progress=_tool_progress(source.get("FEISHU_TOOL_PROGRESS")),
        media_enabled=_bool(source.get("FEISHU_MEDIA_ENABLED"), False),
        dedup_cache_size=_int(source.get("FEISHU_DEDUP_CACHE_SIZE"), 2048, minimum=1),
        dedup_ttl_seconds=_float(source.get("FEISHU_DEDUP_TTL_SECONDS"), 86400.0, minimum=0.0),
        approval_timeout_seconds=_float(
            source.get("FEISHU_APPROVAL_TIMEOUT_SECONDS"), 300.0, minimum=1.0
        ),
    )
    policy = SessionResetPolicy(
        mode=_reset_mode(source.get("GATEWAY_SESSION_RESET_MODE")),
        at_hour=_int(source.get("GATEWAY_SESSION_RESET_AT_HOUR"), 4, minimum=0),
        idle_minutes=_int(source.get("GATEWAY_SESSION_RESET_IDLE_MINUTES"), 1440, minimum=1),
        notify=_bool(source.get("GATEWAY_SESSION_RESET_NOTIFY"), True),
    )
    behavior_raw = source.get("GATEWAY_UNAUTHORIZED_DM_BEHAVIOR", "").strip().lower()
    behavior: UnauthorizedDmBehavior | None = None
    if behavior_raw:
        behavior = _unauthorized(behavior_raw)
    memory_high = source.get("GATEWAY_AGENT_CACHE_MEMORY_HIGH_MB", "").strip()
    return GatewayConfig(
        feishu=feishu,
        reset_policy=policy,
        group_sessions_per_user=_bool(source.get("GATEWAY_GROUP_SESSIONS_PER_USER"), True),
        thread_sessions_per_user=_bool(source.get("GATEWAY_THREAD_SESSIONS_PER_USER"), False),
        unauthorized_dm_behavior=behavior,
        admin_users=_csv(source.get("GATEWAY_ADMIN_USERS")),
        user_allowed_commands=frozenset(
            item.lstrip("/") for item in _csv(source.get("GATEWAY_USER_ALLOWED_COMMANDS"))
        ),
        busy_input_mode=_busy_mode(source.get("GATEWAY_BUSY_INPUT_MODE")),
        busy_queue_max_pending=_int(source.get("GATEWAY_BUSY_QUEUE_MAX_PENDING"), 32, minimum=1),
        agent_idle_seconds=_float(source.get("GATEWAY_AGENT_IDLE_SECONDS"), 3600.0, minimum=1.0),
        max_cached_agents=_int(source.get("GATEWAY_MAX_CACHED_AGENTS"), 128, minimum=1),
        agent_cache_memory_high_mb=int(memory_high) if memory_high else None,
        turn_lease_timeout_seconds=_float(
            source.get("GATEWAY_TURN_LEASE_TIMEOUT_SECONDS"), 1800.0, minimum=1.0
        ),
        agent_timeout_seconds=_float(
            source.get("GATEWAY_AGENT_TIMEOUT_SECONDS"), 1800.0, minimum=0.0
        ),
        agent_timeout_warning_seconds=_float(
            source.get("GATEWAY_AGENT_TIMEOUT_WARNING_SECONDS"), 900.0, minimum=0.0
        ),
        stall_timeout_seconds=_float(source.get("GATEWAY_STALL_SECONDS"), 300.0, minimum=0.0),
        restart_drain_timeout_seconds=_float(
            source.get("GATEWAY_RESTART_DRAIN_TIMEOUT_SECONDS"), 0.0, minimum=0.0
        ),
        auto_continue_freshness_seconds=_float(
            source.get("GATEWAY_AUTO_CONTINUE_FRESHNESS_SECONDS"), 3600.0, minimum=0.0
        ),
        restart_loop_max_boots=_int(source.get("GATEWAY_RESTART_LOOP_MAX_BOOTS"), 3, minimum=1),
        restart_loop_window_seconds=_float(
            source.get("GATEWAY_RESTART_LOOP_WINDOW_SECONDS"), 60.0, minimum=1.0
        ),
        session_expiry_watch_seconds=_float(
            source.get("GATEWAY_SESSION_EXPIRY_WATCH_SECONDS"), 300.0, minimum=1.0
        ),
        heartbeat_poll_seconds=_float(
            source.get("GATEWAY_HEARTBEAT_POLL_SECONDS"), 5.0, minimum=1.0
        ),
        heartbeat_min_interval_seconds=_float(
            source.get("GATEWAY_HEARTBEAT_MIN_INTERVAL_SECONDS"), 60.0, minimum=1.0
        ),
        delivery_ledger_enabled=_bool(source.get("GATEWAY_DELIVERY_LEDGER"), True),
        memory_monitor_seconds=_float(
            source.get("GATEWAY_MEMORY_MONITOR_SECONDS"), 300.0, minimum=0.0
        ),
    )


def _reset_mode(value: str | None) -> ResetMode:
    text = _choice(value, "none", ("none", "idle", "daily", "both"), "GATEWAY_SESSION_RESET_MODE")
    return (
        "none"
        if text == "none"
        else "idle"
        if text == "idle"
        else "daily"
        if text == "daily"
        else "both"
    )


def _reply_mode(value: str | None) -> ReplyToMode:
    text = _choice(value, "first", ("off", "first", "all"), "FEISHU_REPLY_TO_MODE")
    return "off" if text == "off" else "first" if text == "first" else "all"


def _connection_mode(value: str | None) -> ConnectionMode:
    text = _choice(value, "websocket", ("websocket", "webhook", "ws"), "FEISHU_CONNECTION_MODE")
    return "webhook" if text == "webhook" else "websocket"


def _group_policy(value: str | None) -> GroupPolicy:
    text = _choice(
        value,
        "allowlist",
        ("open", "allowlist", "blocklist", "admin_only", "disabled"),
        "FEISHU_GROUP_POLICY",
    )
    if text == "open":
        return "open"
    if text == "blocklist":
        return "blocklist"
    if text == "admin_only":
        return "admin_only"
    if text == "disabled":
        return "disabled"
    return "allowlist"


def _allow_bots(value: str | None) -> AllowBots:
    text = _choice(value, "none", ("none", "mentions", "all"), "FEISHU_ALLOW_BOTS")
    return "mentions" if text == "mentions" else "all" if text == "all" else "none"


def _table_mode(value: str | None) -> TableMode:
    text = _choice(value, "table", ("table", "bullets", "code", "off"), "FEISHU_TABLE_MODE")
    return (
        "bullets"
        if text == "bullets"
        else "code"
        if text == "code"
        else "off"
        if text == "off"
        else "table"
    )


def _tool_progress(value: str | None) -> ToolProgressMode:
    text = _choice(value, "off", ("off", "new", "all", "verbose"), "FEISHU_TOOL_PROGRESS")
    return (
        "new"
        if text == "new"
        else "all"
        if text == "all"
        else "verbose"
        if text == "verbose"
        else "off"
    )


def _busy_mode(value: str | None) -> BusyInputMode:
    text = _choice(value, "interrupt", ("interrupt", "queue", "steer"), "GATEWAY_BUSY_INPUT_MODE")
    return "queue" if text == "queue" else "steer" if text == "steer" else "interrupt"


def _unauthorized(value: str) -> UnauthorizedDmBehavior:
    text = _choice(value, "pair", ("pair", "ignore", "reply"), "GATEWAY_UNAUTHORIZED_DM_BEHAVIOR")
    return "ignore" if text == "ignore" else "reply" if text == "reply" else "pair"


def _group_rules(value: str | None) -> dict[str, GroupRule]:
    """Parse ``FEISHU_GROUP_RULES``: a JSON object keyed by chat id.

    Each value may carry ``policy``, ``allowlist``, ``blocklist`` and ``require_mention``;
    a missing key means "inherit", so ``false`` and "unset" never collapse into each other.
    """
    if not value or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FEISHU_GROUP_RULES is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("FEISHU_GROUP_RULES must be a JSON object keyed by chat id")
    rules: dict[str, GroupRule] = {}
    for chat_id, raw in data.items():
        if not isinstance(raw, dict):
            continue
        policy = raw.get("policy")
        if policy is not None:
            policy = _group_policy(str(policy))
        require = raw.get("require_mention")
        rules[str(chat_id)] = GroupRule(
            policy=policy,
            allowlist=frozenset(str(u).strip() for u in raw.get("allowlist", []) if str(u).strip()),
            blocklist=frozenset(str(u).strip() for u in raw.get("blocklist", []) if str(u).strip()),
            require_mention=None if require is None else bool(require),
        )
    return rules


__all__ = [
    "AllowBots",
    "BusyInputMode",
    "ConnectionMode",
    "FeishuConfig",
    "GatewayConfig",
    "GroupPolicy",
    "GroupRule",
    "ReplyToMode",
    "ResetMode",
    "SessionResetPolicy",
    "TableMode",
    "ToolProgressMode",
    "UnauthorizedDmBehavior",
    "load_gateway_config",
]
