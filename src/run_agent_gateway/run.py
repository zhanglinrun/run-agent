"""The gateway runner: one Feishu adapter, one session store, one coding agent per chat.

The adapter serializes turns per chat and calls ``handle_message``; the runner authorizes
the sender, resolves the chat to a coding session, runs the agent on that session and
returns the reply text. Around that core it adds the pieces that make a long-running
gateway trustworthy:

- a per-session turn lease, so two chats that resolve to the same session cannot
  interleave their writes on one transcript;
- a delivery ledger, so a reply produced just before a crash is resent after restart;
- heartbeats, durable scheduled prompts that wake a chat's session on their own;
- stall detection, one notice when a running turn has stopped making progress.

Open agents are kept in a small cache so a chat's follow-up turns do not pay for a fresh
application start, and are closed after they have been idle for a while.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import ProviderSettings
from run_agent_coding.session_manager import SessionManager
from run_agent_core.events import MessageEndEvent, ToolExecutionEndEvent, ToolExecutionStartEvent
from run_agent_core.messages import AssistantMessage
from run_agent_core.provider import ModelProvider
from run_agent_gateway.approval import (
    ApprovalChoice,
    ApprovalRegistry,
    ApprovalStore,
    DangerVerdict,
    format_exec_approval_text,
    route_approval_reply,
)
from run_agent_gateway.approval_extension import (
    current_approval_session_key,
    reset_approval_gate,
    reset_approval_session_key,
    set_approval_gate,
    set_approval_session_key,
)
from run_agent_gateway.config import GatewayConfig
from run_agent_gateway.heartbeat import HeartbeatJob, HeartbeatScheduler, HeartbeatStore
from run_agent_gateway.lease import SessionTurnLeaseRegistry, TurnLeaseTimeoutError
from run_agent_gateway.ledger import DeliveryLedger
from run_agent_gateway.pairing import PairingStore
from run_agent_gateway.platforms.base import BasePlatformAdapter, MessageEvent
from run_agent_gateway.session import SessionEntry, SessionSource, SessionStore
from run_agent_gateway.stall import StallMonitor, format_stall_notice

logger = logging.getLogger(__name__)

HELP_TEXT = """可用命令：
/new 或 /reset：开始新会话
/stop：停止当前正在执行的任务
/status：查看当前会话状态
/heartbeat add <分钟> <提示词>：按固定间隔自动唤醒本会话执行提示词
/heartbeat once <分钟> <提示词>：延迟一次执行
/heartbeat list、/heartbeat remove <id>：查看或删除心跳
/model [名称]：查看或切换模型
/thinking [级别]：查看或调整思考强度
/compact：压缩当前会话上下文
/help：显示本说明"""


@dataclass(slots=True)
class _CachedAgent:
    application: CodingApplication
    session_id: str
    last_used: float


class GatewayRunner:
    def __init__(
        self,
        config: GatewayConfig,
        options: ApplicationOptions,
        adapter: BasePlatformAdapter,
        *,
        settings: ProviderSettings | None = None,
        provider_factory: Callable[[], ModelProvider] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.options = options
        self.adapter = adapter
        self.settings = settings
        self.provider_factory = provider_factory
        self._clock = clock
        paths = options.paths or RunAgentPaths()
        self.paths = paths
        state = paths.home / "gateway"
        self._approval_state = state
        self.manager = SessionManager(paths)
        self.store = SessionStore(state / "sessions.json", config.reset_policy)
        self.leases = SessionTurnLeaseRegistry()
        self.stalls = StallMonitor(config.stall_timeout_seconds, clock=clock)
        self.ledger = (
            DeliveryLedger(state / "deliveries.sqlite3") if config.delivery_ledger_enabled else None
        )
        self.approvals = ApprovalRegistry(
            ApprovalStore(state / "approvals.json"),
            timeout_seconds=config.feishu.approval_timeout_seconds,
        )
        self.pairing = PairingStore(state / "pairing")
        adapter.ledger = self.ledger
        self.heartbeats = HeartbeatStore(state / "heartbeats.json")
        self._heartbeat_scheduler = HeartbeatScheduler(
            self.heartbeats, self._wake_heartbeat, poll_seconds=config.heartbeat_poll_seconds
        )
        self._agents: OrderedDict[str, _CachedAgent] = OrderedDict()
        self._open_locks: dict[str, asyncio.Lock] = {}
        self._transition_locks: dict[str, asyncio.Lock] = {}
        self._background: list[asyncio.Task[None]] = []
        self.recovered_deliveries = 0
        adapter.set_message_handler(self.handle_message)
        adapter.set_busy_handler(self._steer_busy)
        adapter.set_approval_handler(self._resolve_approval_card)

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if not await self.adapter.connect():
            raise RuntimeError(f"{self.adapter.name} adapter failed to connect")
        await self.redeliver_pending()
        self._background = [
            asyncio.create_task(self._sweep_idle_agents(), name="gateway-sweeper"),
            asyncio.create_task(self._watch_stalls(), name="gateway-stall-watch"),
        ]
        self._heartbeat_scheduler.start()
        logger.info("gateway ready on %s (cwd=%s)", self.adapter.name, self.options.cwd)

    async def stop(self) -> None:
        await self._heartbeat_scheduler.stop()
        for task in self._background:
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._background = []
        try:
            for entry in self.store.entries():
                self.approvals.cancel_session(entry.session_key, reason="gateway stopped")
            await self.adapter.disconnect()
        finally:
            for session_id in list(self._agents):
                await self._close_agent(session_id, cancel=True)
            await self.manager.aclose()

    async def run_until(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    async def redeliver_pending(self) -> int:
        """Resend replies a previous process recorded but never confirmed."""
        if self.ledger is None:
            return 0
        count = 0
        for obligation in self.ledger.sweep_recoverable():
            self.ledger.mark_attempting(obligation.obligation_id)
            result = await self.adapter.send_with_retry(
                obligation.chat_id,
                obligation.recovered_content,
                reply_to=obligation.reply_to,
                thread_id=obligation.thread_id,
            )
            if result.success:
                self.ledger.mark_delivered(obligation.obligation_id)
                count += 1
            else:
                if result.error_kind == "unknown" or "timed out" in (result.error or "").lower():
                    self.ledger.mark_unknown(
                        obligation.obligation_id, result.error or "delivery outcome unknown"
                    )
                else:
                    self.ledger.mark_failed(obligation.obligation_id, result.error or "send failed")
        self.recovered_deliveries += count
        if count:
            logger.info("redelivered %d reply(ies) left over from a previous run", count)
        return count

    # -- message handling -----------------------------------------------------------

    async def handle_message(self, event: MessageEvent) -> str | None:
        source = event.source
        if not event.internal:
            if source.chat_type == "group":
                if not self.config.feishu.allows_group_message(source.user_id, source.chat_id):
                    return None
            elif not self.config.feishu.is_authorized(source.user_id):
                behavior = self.config.effective_unauthorized_dm_behavior
                if behavior == "pair":
                    if self.pairing.is_approved(self.adapter.name, source.user_id):
                        pass
                    else:
                        code = self.pairing.generate_code(
                            self.adapter.name,
                            source.user_id or "",
                            source.user_name or "",
                        )
                        if code is None:
                            return "配对请求已限流或待处理请求已满，请稍后再试。"
                        return f"此账号尚未授权。请将以下一次性配对码交给管理员批准：\n`{code}`"
                elif behavior == "ignore":
                    return None
                else:
                    logger.warning("unauthorized sender %s", source.user_id)
                    return (
                        "你还没有被授权使用这个机器人。\n"
                        f"你的 open_id 是 `{source.user_id or '未知'}`，"
                        "请让管理员把它加入 FEISHU_ALLOWED_USERS。"
                    )
        key = self.adapter.session_key_for(source)
        entry = self.store.get_or_create(key, source)
        routed_approval = (
            route_approval_reply(event.text) if self.approvals.has_blocking(key) else None
        )
        command = event.get_command()
        if routed_approval is not None:
            return await self._handle_approval_text(routed_approval, key, source)
        if command is not None:
            return await self._handle_command(command, event, key, entry)
        return await self._run_turn(event, key, entry)

    def _command_allowed(self, command: str, user_id: str | None) -> bool:
        """Apply the optional command role split after global identity admission."""
        if not self.config.slash_access_enabled:
            return True
        if self.config.feishu.is_admin(user_id) or user_id in self.config.admin_users:
            return True
        return command in self.config.user_allowed_commands

    async def _handle_approval_text(self, text: str, key: str, source: SessionSource) -> str:
        command, _, args = text.partition(" ")
        return await self._resolve_approval_command(command[1:], args.strip(), key, source.user_id)

    async def _resolve_approval_command(
        self, command: str, arguments: str, key: str, user_id: str | None
    ) -> str:
        if command == "deny":
            choice: ApprovalChoice = "deny"
        elif command == "approve":
            normalized = arguments.lower()
            choice = cast(
                ApprovalChoice,
                normalized if normalized in {"session", "always"} else "once",
            )
        else:
            return "审批回复无法识别。"
        resolved = self.approvals.resolve(key, choice, resolved_by=user_id)
        return (
            "已批准当前危险操作。"
            if choice != "deny" and resolved
            else (
                "已拒绝当前危险操作。"
                if choice == "deny" and resolved
                else "当前没有等待审批的操作。"
            )
        )

    async def _resolve_approval_card(
        self,
        approval_id: str,
        choice: str,
        user_id: str | None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        pending = self.approvals.get(approval_id)
        if pending is None or pending.chat_id != chat_id:
            return
        if pending.thread_id is not None and pending.thread_id != thread_id:
            return
        if pending.chat_type == "group" and not self.config.feishu.allows_group_message(
            user_id, pending.chat_id
        ):
            return
        authorized = self.config.feishu.is_authorized(user_id) or self.pairing.is_approved(
            self.adapter.name, user_id
        )
        if not authorized:
            return
        normalized = choice.lower()
        if normalized not in {"once", "session", "always", "deny"}:
            return
        self.approvals.resolve_by_id(
            approval_id, cast(ApprovalChoice, normalized), resolved_by=user_id
        )

    async def _approval_gate(self, command: str, verdict: DangerVerdict) -> str | None:
        session_key = current_approval_session_key()
        if session_key is None:
            return "gateway approval context is unavailable"
        if self.approvals.store.is_allowed(session_key, verdict):
            return None
        approval_entry = self.store.get(session_key)
        if approval_entry is None:
            return "gateway approval context is unavailable"
        pending = self.approvals.create(
            session_key,
            command,
            verdict,
            chat_id=approval_entry.origin.chat_id if approval_entry is not None else None,
            thread_id=approval_entry.origin.thread_id if approval_entry is not None else None,
            chat_type=approval_entry.origin.chat_type if approval_entry is not None else None,
        )
        card = await self.adapter.send_approval_card(
            approval_entry.origin.chat_id if approval_entry is not None else "",
            approval_id=pending.approval_id,
            command=command,
            description=pending.description,
        )
        if not card.success:
            entry = self.store.get(session_key)
            if entry is not None:
                await self.adapter.send(
                    entry.origin.chat_id,
                    format_exec_approval_text(command, pending.description),
                    reply_to=entry.origin.message_id,
                    thread_id=entry.origin.thread_id,
                )
        choice = await self.approvals.wait(pending)
        if choice in {"once", "session", "always"}:
            return None
        return pending.reason or "operation denied by user"

    async def _steer_busy(self, event: MessageEvent, key: str) -> bool:
        entry = self.store.get(key)
        agent = self._agents.get(entry.session_id) if entry is not None else None
        if agent is None or not agent.application.session.is_running:
            return False
        text = event.text
        if event.source.chat_type == "group" and event.source.user_name:
            text = f"[{event.source.user_name}] {text}"
        agent.application.session.queue_steering_message(text)
        return True

    async def _handle_command(
        self, command: str, event: MessageEvent, key: str, entry: SessionEntry
    ) -> str | None:
        if not self._command_allowed(command, event.source.user_id):
            return f"你没有权限执行 /{command}。"
        if command == "pair":
            return self._pair_command(event.get_command_args(), event.source.user_id)
        if command in {"approve", "deny"}:
            return await self._resolve_approval_command(
                command, event.get_command_args(), key, event.source.user_id
            )
        if command in {"new", "reset", "resume"}:
            self.approvals.cancel_session(key, reason=f"/{command} changed the session")
        if command in {"new", "reset"}:
            self.adapter.begin_transition(key)
            try:
                async with self._transition_locks.setdefault(key, asyncio.Lock()):
                    await self._discard_pending(key)
                    stopped = await self.adapter.cancel_active(key)
                    await self._discard_pending(key)
                    if not stopped:
                        return "当前任务尚未停止，未切换会话，请稍后重试。"
                    holder = self.leases.holder(entry.session_id)
                    if holder in {None, key}:
                        await self._close_agent(entry.session_id, cancel=True)
                    await self._discard_pending(key)
                    fresh = self.store.reset(key, event.source)
                    return f"已开始新会话（{fresh.session_id[:8]}）。"
            finally:
                self.adapter.end_transition(key)
        if command == "stop":
            self.adapter.begin_transition(key)
            try:
                async with self._transition_locks.setdefault(key, asyncio.Lock()):
                    self.approvals.cancel_session(key, reason="session stopped")
                    discarded = await self._discard_pending(
                        key, message="排队消息已因停止取消，请重新发送。"
                    )
                    agent = self._agents.get(entry.session_id)
                    if agent is not None and agent.application.session.is_running:
                        agent.application.session.cancel()
                        stopped = await self.adapter.cancel_active(key)
                        discarded += await self._discard_pending(key)
                        if stopped:
                            return f"正在停止当前任务，已取消 {discarded} 条排队消息。"
                        return "已请求停止，任务仍在收敛。"
                    discarded += await self._discard_pending(key)
                    if discarded:
                        return f"当前没有正在执行的任务，已取消 {discarded} 条排队消息。"
                    return "当前没有正在执行的任务。"
            finally:
                self.adapter.end_transition(key)
        if command == "status":
            return self._status_text(key, entry)
        if command == "help":
            return HELP_TEXT
        if command == "heartbeat":
            return self._heartbeat_command(event.get_command_args(), key, event.source)
        agent = await self._agent_for(entry)
        previous_session_id = entry.session_id
        result = await agent.application.command(event.text.strip())
        current_session_id = agent.application.session.session_id
        if result.handled and current_session_id and current_session_id != previous_session_id:
            try:
                self.store.replace(key, current_session_id)
            except Exception as exc:
                logger.exception("persisting resumed route failed for %s", key)
                await self._close_agent(previous_session_id, cancel=False)
                return f"会话已恢复但路由保存失败，已保持原会话：{exc}"
            self._agents.pop(previous_session_id, None)
            agent.session_id = current_session_id
            self._agents[current_session_id] = agent
        if not result.handled:
            return f"未知命令 /{command}。发送 /help 查看可用命令。"
        return result.message or None

    async def _discard_pending(self, key: str, *, message: str | None = None) -> int:
        count = 0
        text = message or "这条消息因会话已切换而取消，请在新会话中重新发送。"
        for event in self.adapter.clear_pending(key):
            count += 1
            await self.adapter.deliver_reply(
                key,
                event.source.chat_id,
                text,
                reply_to=event.message_id,
                thread_id=event.source.thread_id,
            )
        return count

    def _pair_command(self, arguments: str, user_id: str | None) -> str:
        if not (self.config.feishu.is_admin(user_id) or user_id in self.config.admin_users):
            return "只有管理员可以处理配对请求。"
        action, _, value = arguments.strip().partition(" ")
        if action.lower() == "approve" and value.strip():
            result = self.pairing.approve_code(self.adapter.name, value)
            return (
                f"已批准用户 {result['user_id']}。"
                if result is not None
                else "配对码无效、已过期或已被锁定。"
            )
        pending = self.pairing.list_pending(self.adapter.name)
        if not pending:
            return "没有待处理的配对请求。"
        return "\n".join(
            f"{item['user_id']} ({item['user_name'] or 'unknown'})" for item in pending
        )

    def _status_text(self, key: str, entry: SessionEntry) -> str:
        agent = self._agents.get(entry.session_id)
        lines = [f"会话：{entry.session_id[:8]}", f"来源：{entry.origin.description}"]
        if agent is not None:
            session = agent.application.session
            lines.append(f"模型：{session.provider_name}:{session.model}")
            lines.append(f"思考强度：{session.thinking_level}")
            lines.append("状态：" + ("执行中" if session.is_running else "空闲"))
        else:
            lines.append(f"模型：{self.options.provider_name or ''}:{self.options.model or ''}")
            lines.append("状态：空闲")
        activity = self.stalls.activity(key)
        if activity is not None:
            idle = self._clock() - activity.last_progress_at
            lines.append(f"本轮进展：{activity.events} 个事件，{idle:.0f} 秒前有更新")
        queued = self.adapter.pending_count(key)
        if queued:
            lines.append(f"排队消息：{queued}")
        jobs = self.heartbeats.for_session(key)
        if jobs:
            lines.append(f"心跳任务：{len(jobs)}")
        return "\n".join(lines)

    def _heartbeat_command(self, arguments: str, key: str, source: SessionSource) -> str:
        action, _, rest = arguments.strip().partition(" ")
        action = action.lower()
        if action == "list" or not action:
            jobs = self.heartbeats.for_session(key)
            if not jobs:
                return "本会话没有心跳任务。用 /heartbeat add <分钟> <提示词> 创建一个。"
            return "\n".join(job.describe() for job in jobs)
        if action == "remove":
            job = self.heartbeats.get(rest.strip())
            if job is None or job.session_key != key:
                return "没有找到这个心跳任务。"
            self.heartbeats.remove(job.job_id)
            return f"已删除心跳 {job.job_id}。"
        if action in {"add", "once"}:
            minutes_text, _, prompt = rest.strip().partition(" ")
            try:
                minutes = float(minutes_text)
            except ValueError:
                return "用法：/heartbeat add|once <分钟> <提示词>"
            if minutes < 1 or not prompt.strip():
                return "间隔至少 1 分钟，并且需要提示词。"
            interval = minutes * 60
            try:
                job = self.heartbeats.add(
                    key,
                    replace(source, message_id=None),
                    prompt,
                    interval_seconds=interval if action == "add" else None,
                    first_run_at=time.time() + interval,
                )
            except ValueError as exc:
                return str(exc)
            return f"已创建心跳 {job.job_id}：{job.describe()}"
        return "用法：/heartbeat add|once <分钟> <提示词>，/heartbeat list，/heartbeat remove <id>"

    async def _wake_heartbeat(self, job: HeartbeatJob) -> None:
        """Inject the job's prompt as an internal message on the job's chat."""
        text = f"[定时任务 {job.job_id}] {job.prompt}"
        await self.adapter.handle_message(
            MessageEvent(
                text=text, source=job.source, internal=True, metadata={"heartbeat": job.job_id}
            )
        )
        await self.adapter.wait_for_idle(job.session_key)

    async def _run_turn(self, event: MessageEvent, key: str, entry: SessionEntry) -> str:
        text = event.text
        if event.source.chat_type == "group" and event.source.user_name and not event.internal:
            text = f"[{event.source.user_name}] {text}"
        try:
            token = await self.leases.acquire(
                entry.session_id, key, timeout=self.config.turn_lease_timeout_seconds
            )
        except TurnLeaseTimeoutError as exc:
            logger.warning("turn lease timeout: %s", exc)
            return "这个会话正被另一个聊天占用，请稍后重发这条消息。"
        self.stalls.begin(key)
        try:
            agent = await self._agent_for(entry)
            reply = await self._run_agent(agent.application, text, key)
            agent.last_used = self._clock()
        finally:
            self.stalls.end(key)
            await self.leases.release(token)
        self.store.touch(key)
        if entry.was_auto_reset and self.config.reset_policy.notify:
            entry.was_auto_reset = False
            reason = "长时间未活动" if entry.auto_reset_reason == "idle" else "到了每日重置时间"
            reply = f"（由于{reason}，已自动开始新会话。）\n\n{reply}"
        return reply

    async def _run_agent(self, application: CodingApplication, text: str, key: str) -> str:
        replies: list[str] = []
        error: str | None = None
        status: str | None = None
        gate_token = set_approval_gate(self._approval_gate)
        session_token = set_approval_session_key(key)
        try:
            async for event in application.prompt(text):
                label = ""
                if isinstance(event, ToolExecutionStartEvent):
                    label = f"调用 {event.tool_name}"
                elif isinstance(event, ToolExecutionEndEvent):
                    label = f"{event.tool_name} 完成"
                self.stalls.progress(key, label)
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    message = event.message
                    if message.stop_reason == "error":
                        error = message.error_message or "模型请求失败"
                    elif message.stop_reason != "toolUse" and message.text.strip():
                        replies.append(message.text.strip())
                elif isinstance(event, AgentSettledEvent):
                    status = event.status
        finally:
            reset_approval_session_key(session_token)
            reset_approval_gate(gate_token)
        if error and not replies:
            return f"本轮出错：{error}"
        if status == "cancelled" and not replies:
            return "任务已停止。"
        if not replies:
            return "（本轮没有文本回复。）"
        return "\n\n".join(replies)

    async def _watch_stalls(self) -> None:
        interval = min(30.0, max(1.0, self.config.stall_timeout_seconds / 10))
        while True:
            await asyncio.sleep(interval)
            await self.check_stalls()

    async def check_stalls(self) -> int:
        """Send one notice per stalled turn; returns how many were sent."""
        sent = 0
        for report in self.stalls.stalled():
            entry = self.store.get(report.session_key)
            if entry is None:
                continue
            origin = entry.origin
            result = await self.adapter.send_with_retry(
                origin.chat_id, format_stall_notice(report), thread_id=origin.thread_id
            )
            sent += int(result.success)
        return sent

    # -- agent cache ----------------------------------------------------------------

    async def _agent_for(self, entry: SessionEntry) -> _CachedAgent:
        """One open application per coding session, whichever chats map to it."""
        session_id = entry.session_id
        lock = self._open_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            cached = self._agents.get(session_id)
            if cached is not None:
                self._agents.move_to_end(session_id)
                cached.last_used = self._clock()
                return cached
            await self._enforce_cache_cap()
            application = await self._open(entry)
            agent = _CachedAgent(application, session_id, self._clock())
            self._agents[session_id] = agent
            return agent

    async def _open(self, entry: SessionEntry) -> CodingApplication:
        record = await self.manager.get_session(entry.session_id)
        options = (
            replace(self.options, resume=entry.session_id)
            if record is not None
            else replace(self.options, session_id=entry.session_id)
        )
        approval_extension = Path(__file__).with_name("approval_extension.py")
        options = replace(
            options,
            extension_paths=tuple(dict.fromkeys((*options.extension_paths, approval_extension))),
        )
        provider = self.provider_factory() if self.provider_factory is not None else None
        application = await CodingApplication.open(
            options, manager=self.manager, provider=provider, settings=self.settings
        )
        try:
            await application.start()
        except BaseException:
            await application.aclose()
            raise
        return application

    async def _close_agent(self, session_id: str, *, cancel: bool) -> None:
        agent = self._agents.pop(session_id, None)
        if agent is None:
            return
        session = agent.application.session
        for entry in self.store.entries():
            if entry.session_id == session_id:
                self.approvals.cancel_session(entry.session_key, reason="agent closed")
        if session.is_running:
            if cancel:
                session.cancel()
            deadline = self._clock() + 10
            while session.is_running and self._clock() < deadline:
                await asyncio.sleep(0.05)
        try:
            await agent.application.aclose()
        except Exception:
            logger.exception("closing agent for session %s failed", session_id)

    async def _enforce_cache_cap(self) -> None:
        while len(self._agents) >= self.config.max_cached_agents:
            victim = next(
                (
                    sid
                    for sid, a in self._agents.items()
                    if not a.application.session.is_running and not self.leases.is_held(sid)
                ),
                None,
            )
            if victim is None:
                return
            await self._close_agent(victim, cancel=False)

    async def _sweep_idle_agents(self) -> None:
        interval = min(60.0, max(1.0, self.config.agent_idle_seconds / 4))
        while True:
            await asyncio.sleep(interval)
            await self.sweep_idle_agents()

    async def sweep_idle_agents(self) -> int:
        """Close agents that have been idle longer than the configured limit."""
        now = self._clock()
        for agent in tuple(self._agents.values()):
            await agent.application.session.extension_runtime.tick_maintenance(
                idle_seconds=max(0.0, now - agent.last_used)
            )
        idle = [
            sid
            for sid, agent in list(self._agents.items())
            if not agent.application.session.is_running
            and not self.leases.is_held(sid)
            and now - agent.last_used >= self.config.agent_idle_seconds
        ]
        for sid in idle:
            await self._close_agent(sid, cancel=False)
        return len(idle)

    @property
    def cached_session_ids(self) -> tuple[str, ...]:
        return tuple(self._agents)


__all__ = ["HELP_TEXT", "GatewayRunner"]
