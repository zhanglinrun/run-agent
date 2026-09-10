"""Bounded channel host with durable admission, independent controls and Outbox sending."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from run_agent_core.types import JSONValue
from run_agent_gateway.contracts import AdmissionRejected, RouteIdentity, Submission
from run_agent_gateway.controller import CONTROL_COMMANDS, SessionController
from run_agent_gateway.identity import IdentityPolicy
from run_agent_gateway.outbox import Delivery, OutboxRepository
from run_agent_gateway.routing import route_key
from run_agent_gateway.scheduler import GatewayScheduler


@dataclass(frozen=True, slots=True)
class InboundMessage:
    source_message_id: str
    account_id: str
    sender_id: str
    chat_id: str
    text: str
    thread_id: str = ""
    metadata: dict[str, JSONValue] = field(default_factory=dict)


class GatewayAdapter(Protocol):
    @property
    def name(self) -> str: ...
    def messages(self) -> AsyncIterator[InboundMessage]: ...
    async def send(self, delivery: Delivery) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class BoundedIngress:
    """SDK callbacks enqueue synchronously; overload is explicit and allocates no tasks."""

    def __init__(self, *, ordinary_capacity: int = 128, control_capacity: int = 32) -> None:
        if ordinary_capacity < 1 or control_capacity < 1:
            raise ValueError("Ingress capacities must be positive")
        self._ordinary: asyncio.Queue[tuple[int, InboundMessage]] = asyncio.Queue(ordinary_capacity)
        self._control: asyncio.Queue[tuple[int, InboundMessage]] = asyncio.Queue(control_capacity)
        self._sequence = 0
        self._wake = asyncio.Event()
        self._closed = False

    def put(self, message: InboundMessage) -> None:
        if self._closed:
            raise AdmissionRejected("Adapter input is closed")
        command = message.text.strip().partition(" ")[0]
        queue = self._control if command in CONTROL_COMMANDS else self._ordinary
        self._sequence += 1
        try:
            queue.put_nowait((self._sequence, message))
        except asyncio.QueueFull as exc:
            raise AdmissionRejected(
                "Adapter input capacity is full; message was not accepted"
            ) from exc
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            self._wake.clear()
            queue = self._control if not self._control.empty() else self._ordinary
            if not queue.empty():
                sequence, message = queue.get_nowait()
                if message.text.strip().partition(" ")[0] in {"/stop", "/new", "/steer"}:
                    # A destructive control cannot move earlier inputs into the next
                    # epoch. Drain their short admissions before applying the barrier.
                    # Include other senders because the host may configure shared chats.
                    # Idle steering becomes ordinary input and keeps arrival order too.
                    before: list[InboundMessage] = []
                    remaining: list[tuple[int, InboundMessage]] = []
                    while not self._ordinary.empty():
                        order, pending = self._ordinary.get_nowait()
                        if order < sequence and (
                            pending.account_id,
                            pending.chat_id,
                            pending.thread_id,
                        ) == (message.account_id, message.chat_id, message.thread_id):
                            before.append(pending)
                        else:
                            remaining.append((order, pending))
                    for item in remaining:
                        self._ordinary.put_nowait(item)
                    for pending in before:
                        yield pending
                yield message
            elif self._closed:
                return
            else:
                await self._wake.wait()


class AgentGateway:
    def __init__(
        self,
        scheduler: GatewayScheduler,
        adapters: Sequence[GatewayAdapter],
        identity_policy: IdentityPolicy,
        *,
        model: str,
        provider_name: str | None = None,
        send_timeout: float = 15,
        prepare_background: Callable[[Submission], Awaitable[None]] | None = None,
    ) -> None:
        if len({adapter.name for adapter in adapters}) != len(adapters):
            raise ValueError("Gateway adapter instance names must be unique")
        if send_timeout <= 0:
            raise ValueError("Send timeout must be positive")
        self.scheduler, self.policy = scheduler, identity_policy
        self.repository, self.owner = scheduler.repository, scheduler.owner
        self.controller = SessionController(self.repository)
        self.outbox = OutboxRepository(self.repository)
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.model, self.provider_name = model, provider_name
        self.send_timeout = send_timeout
        self.prepare_background = prepare_background
        self._consumers: list[asyncio.Task[None]] = []
        self._preparations: set[asyncio.Task[None]] = set()
        self._preparation_monitors: set[asyncio.Task[None]] = set()
        self._preparation_failure: BaseException | None = None
        self._sender: asyncio.Task[None] | None = None
        self._rejection_sender: asyncio.Task[None] | None = None
        self._rejected: asyncio.Queue[tuple[GatewayAdapter, Delivery]] = asyncio.Queue(32)
        self._delivery_wake = asyncio.Event()
        self._closing = False
        self._started = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.rejections: list[str] = []

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Gateway already started")
        await self.controller.reconcile(self.owner)
        await self.scheduler.start()
        self._started = True
        self._sender = asyncio.create_task(self._deliver(), name="gateway-outbox")
        self._rejection_sender = asyncio.create_task(
            self._send_rejections(), name="gateway-rejections"
        )
        self._consumers = [
            asyncio.create_task(self._consume(adapter), name=f"gateway-input:{adapter.name}")
            for adapter in self.adapters.values()
        ]

    async def _consume(self, adapter: GatewayAdapter) -> None:
        async for message in adapter.messages():
            if self._closing:
                return
            if message.text.strip().partition(" ")[0] == "/background":
                try:
                    if not message.text.strip().partition(" ")[2].strip():
                        raise ValueError("Usage: /background <content>")
                    rule, route = self.policy.resolve(
                        adapter.name,
                        message.account_id,
                        message.sender_id,
                        message.chat_id,
                        message.thread_id,
                    )
                    anchor = await self.repository.background_anchor(
                        self.owner,
                        Submission(
                            route,
                            rule.principal_id,
                            message.source_message_id,
                            message.text,
                            rule.workspace,
                        ),
                        model=self.model,
                        provider_name=self.provider_name,
                    )
                except (PermissionError, ValueError, KeyError, RuntimeError) as exc:
                    self._reject(adapter, message, exc)
                    continue
                if anchor[2] == -1:
                    await self._receive(adapter, message, expected_route=anchor)
                    continue
                if len(self._preparations) >= 2:
                    from run_agent_coding.storage.settle import settle

                    _, cancelled = await settle(
                        self.repository.release_background_anchor(self.owner, route)
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                    self._reject(
                        adapter, message, AdmissionRejected("Background preparation is full")
                    )
                    continue
                task = asyncio.create_task(
                    self._receive(adapter, message, expected_route=anchor),
                    name="gateway-background-admission",
                )
                self._preparations.add(task)
                monitor = asyncio.create_task(self._finish_preparation(task, route))
                self._preparation_monitors.add(monitor)
                monitor.add_done_callback(self._preparation_monitors.discard)
            else:
                await self._receive(adapter, message)

    async def _finish_preparation(self, task: asyncio.Task[None], route: RouteIdentity) -> None:
        # This monitor is drained, never cancelled. It releases the reservation even
        # when the admission coroutine was cancelled before its first instruction.
        from run_agent_coding.storage.settle import settle

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._preparation_failure = exc
        finally:
            try:
                await settle(self.repository.release_background_anchor(self.owner, route))
            except Exception as exc:
                self._preparation_failure = exc
            finally:
                self._preparations.discard(task)
                self.scheduler.wake()

    async def _receive(
        self,
        adapter: GatewayAdapter,
        message: InboundMessage,
        *,
        expected_route: tuple[str, int, int] | None = None,
    ) -> None:
        try:
            if self._closing:
                return
            rule, route = self.policy.resolve(
                adapter.name,
                message.account_id,
                message.sender_id,
                message.chat_id,
                message.thread_id,
            )
            text = message.text.strip()
            command, _, argument = text.partition(" ")
            submission = Submission(
                route,
                rule.principal_id,
                message.source_message_id,
                argument.strip() if command == "/queue" else text,
                rule.workspace,
                metadata=message.metadata,
            )
            if command in CONTROL_COMMANDS:
                await self.controller.handle(
                    self.owner, submission, model=self.model, provider_name=self.provider_name
                )
            elif command == "/background":
                if not argument.strip():
                    raise ValueError("Usage: /background <content>")
                if self.prepare_background is None:
                    raise AdmissionRejected("This host has no background workspace service")
                submission = replace(submission, content=argument.strip(), lane="background")
                if expected_route is None or expected_route[2] != -1:
                    await self.prepare_background(submission)
                await self.repository.admit(
                    self.owner,
                    submission,
                    model=self.model,
                    provider_name=self.provider_name,
                    expected_route=expected_route,
                )
            else:
                await self.repository.admit(
                    self.owner, submission, model=self.model, provider_name=self.provider_name
                )
            self.scheduler.wake()
            self._delivery_wake.set()
        except (PermissionError, ValueError, KeyError, RuntimeError, OSError) as exc:
            self._reject(adapter, message, exc)

    def _reject(self, adapter: GatewayAdapter, message: InboundMessage, exc: Exception) -> None:
        self.rejections.append(str(exc))
        del self.rejections[:-64]
        route = RouteIdentity(
            adapter.name, message.account_id, message.chat_id, message.thread_id, message.sender_id
        )
        delivery = Delivery(
            "rejected-"
            + hashlib.sha256((route_key(route) + message.source_message_id).encode()).hexdigest(),
            None,
            "rejected",
            {
                "source_message_id": message.source_message_id,
                "account_id": message.account_id,
                "chat_id": message.chat_id,
                "thread_id": message.thread_id,
                "adapter_instance_id": adapter.name,
            },
            {"status": "rejected", "error": str(exc)},
            1,
        )
        try:
            self._rejected.put_nowait((adapter, delivery))
        except asyncio.QueueFull:
            self.rejections[-1] += "; rejection delivery queue full"

    async def _send_rejections(self) -> None:
        while True:
            adapter, delivery = await self._rejected.get()
            try:
                async with asyncio.timeout(self.send_timeout):
                    await adapter.send(delivery)
            except Exception:
                pass

    async def _deliver(self) -> None:
        while True:
            self._delivery_wake.clear()
            deliveries = await self.outbox.claim(self.owner, limit=16)
            if deliveries:
                # A fixed batch bounds tasks even when a channel is unavailable.
                await asyncio.gather(*(self._send(delivery) for delivery in deliveries))
                continue
            if self._closing:
                return
            try:
                async with asyncio.timeout(0.25):
                    await self._delivery_wake.wait()
            except TimeoutError:
                pass

    async def _send(self, delivery: Delivery) -> None:
        adapter = self.adapters.get(str(delivery.destination.get("adapter_instance_id")))
        if adapter is None:
            await self.outbox.fail(self.owner, delivery, "Destination adapter unavailable")
            return
        try:
            async with asyncio.timeout(self.send_timeout):
                receipt = await adapter.send(delivery)
        except Exception as exc:
            await self.outbox.fail(self.owner, delivery, str(exc))
        else:
            await self.outbox.acknowledge(self.owner, delivery, receipt)

    async def wait_closed(self) -> None:
        while (
            any(not task.done() for task in self._consumers)
            or self._preparations
            or self._preparation_monitors
        ):
            if self._preparation_failure is not None:
                raise RuntimeError(
                    "Gateway background preparation failed"
                ) from self._preparation_failure
            if self.scheduler.failure is not None:
                raise RuntimeError("Gateway scheduler failed") from self.scheduler.failure
            if self._sender is not None and self._sender.done():
                await self._sender
                raise RuntimeError("Gateway delivery worker exited unexpectedly")
            for task in self._consumers:
                if task.done():
                    task.result()
            await asyncio.sleep(0.1)
        await asyncio.gather(*self._consumers)
        if self._preparation_failure is not None:
            raise RuntimeError(
                "Gateway background preparation failed"
            ) from self._preparation_failure

    async def shutdown(self, *, grace_period: float = 5) -> None:
        from run_agent_coding.storage.settle import settle

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown(grace_period))

        async def close() -> None:
            assert self._shutdown_task is not None
            await self._shutdown_task

        await settle(close())

    async def _shutdown(self, grace_period: float) -> None:
        try:
            await self._shutdown_owned(grace_period)
        finally:
            await self.scheduler.close_lease()

    async def _shutdown_owned(self, grace_period: float) -> None:
        # Keep channels available until outcomes have committed and sending has drained.
        await self.repository.stop_accepting(self.owner)
        for task in self._consumers:
            task.cancel()
        await asyncio.gather(*self._consumers, return_exceptions=True)
        preparing = tuple(self._preparations)
        for task in preparing:
            task.cancel()
        await asyncio.gather(*preparing, return_exceptions=True)
        await asyncio.gather(*self._preparation_monitors)
        try:
            await self.scheduler.shutdown(grace_period=grace_period, retain_lease=True)
            await self.controller.reconcile(self.owner)
        finally:
            self._closing = True
            self._delivery_wake.set()
            try:
                if self._sender is not None:
                    _, pending = await asyncio.wait({self._sender}, timeout=max(0.1, grace_period))
                    if pending:
                        self._sender.cancel()
                        _, pending = await asyncio.wait(pending, timeout=max(0.1, grace_period))
                    if pending:
                        raise RuntimeError("Gateway delivery worker did not exit")
                    if not self._sender.cancelled():
                        self._sender.result()
            finally:
                if self._rejection_sender is not None:
                    self._rejection_sender.cancel()
                    await asyncio.gather(self._rejection_sender, return_exceptions=True)
                results = await asyncio.gather(
                    *(adapter.close() for adapter in self.adapters.values()), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
        await self.repository.release_owner(self.owner)


class QueueGatewayAdapter:
    """Bounded local adapter with channel-style delivery idempotency."""

    def __init__(self, name: str, *, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("Adapter capacity must be positive")
        self.name = name
        self.ingress = BoundedIngress(ordinary_capacity=capacity)
        self._outgoing: asyncio.Queue[Delivery] = asyncio.Queue(capacity)
        self._sent: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._closed = False

    async def receive_message(self, message: InboundMessage) -> None:
        self.ingress.put(message)

    async def next_sent(self) -> Delivery:
        return await self._outgoing.get()

    async def send(self, delivery: Delivery) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Adapter is closed")
        previous = self._sent.get(delivery.delivery_id)
        if previous is not None:
            return previous
        await self._outgoing.put(delivery)
        receipt = {"message_id": delivery.delivery_id}
        self._sent[delivery.delivery_id] = receipt
        if len(self._sent) > 4096:
            self._sent.popitem(last=False)
        return receipt

    async def close(self) -> None:
        self._closed = True
        self.ingress.close()

    def messages(self) -> AsyncIterator[InboundMessage]:
        return self.ingress.messages()
