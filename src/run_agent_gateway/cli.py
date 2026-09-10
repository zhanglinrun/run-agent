"""Command-line gateway host for trusted channel extensions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from run_agent_coding.application import ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import load_provider_settings
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.thinking import normalize_thinking_level
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.contracts import GatewayLimits
from run_agent_gateway.extensions import GatewayExtensionHost
from run_agent_gateway.gateway import AgentGateway
from run_agent_gateway.identity import IdentityPolicy
from run_agent_gateway.ownership import GatewayProcessLock
from run_agent_gateway.recovery import GatewayRecovery
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.scheduler import GatewayScheduler


async def run_gateway(args: argparse.Namespace) -> None:
    cwd = args.cwd.resolve()
    load_dotenv(cwd / ".env", override=False)
    policy = IdentityPolicy.load(args.identity_map)
    paths = RunAgentPaths(home=args.state_dir.resolve()) if args.state_dir else RunAgentPaths()
    settings = load_provider_settings(paths)
    thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    provider = settings.get_provider(args.provider)
    options = ApplicationOptions(
        cwd=cwd,
        paths=paths,
        provider_name=provider.name,
        model=args.model or os.environ.get("MODEL") or provider.default_model,
        thinking=(normalize_thinking_level(thinking) if thinking else None),
        extension_paths=tuple(path.resolve() for path in args.agent_extension),
        project_extensions_enabled=args.project_extensions,
        trust_default="always" if args.trust_project else "never",
    )
    lock = GatewayProcessLock(paths.home / "gateway.lock")
    lock.acquire()
    try:
        async with await SqliteDatabase.open(paths.database_path) as database:
            repository = GatewayRepository(
                database,
                limits=GatewayLimits(
                    running_total=args.running_total,
                    running_foreground_reserved=args.foreground_reserved,
                    running_background_reserved=args.background_reserved,
                ),
            )
            await repository.initialize()
            owner = await repository.acquire_owner(uuid4().hex, process_lock=lock)
            await GatewayRecovery(repository, owner).reconcile()
            host = GatewayExtensionHost()
            adapters = host.load(args.extension)
            if not adapters:
                await repository.release_owner(owner)
                raise ValueError("Gateway requires at least one registered adapter")
            runtime = GatewayCodingRuntime(repository, owner, options, settings=settings)
            scheduler = GatewayScheduler(repository, owner, CodingAssignmentRunner(runtime))
            gateway = AgentGateway(
                scheduler,
                adapters,
                policy,
                model=options.model or "",
                provider_name=options.provider_name,
                prepare_background=runtime.prepare_background,
            )
            try:
                await gateway.start()
                await gateway.wait_closed()
            finally:
                await gateway.shutdown(grace_period=args.grace_period)
    finally:
        lock.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run gateway",
        description="Run the session-aware Run Agent gateway with trusted adapter extensions.",
    )
    parser.add_argument("--extension", type=Path, action="append", default=[], required=True)
    parser.add_argument("--identity-map", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--agent-extension", type=Path, action="append", default=[])
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument("--running-total", type=int, default=8)
    parser.add_argument("--foreground-reserved", type=int, default=4)
    parser.add_argument("--background-reserved", type=int, default=1)
    parser.add_argument("--grace-period", type=float, default=5.0)
    parser.add_argument("--project-extensions", action="store_true")
    parser.add_argument("--trust-project", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "recover":
        return recovery_main(argv[1:])
    args = _parser().parse_args(argv)
    try:
        asyncio.run(run_gateway(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Gateway failed: {exc}") from exc
    return 0


async def _recover(args: argparse.Namespace) -> list[dict[str, object]]:
    paths = RunAgentPaths(home=args.state_dir.resolve()) if args.state_dir else RunAgentPaths()
    if not paths.database_path.is_file():
        raise ValueError("Recovery requires an existing state database")
    lock = GatewayProcessLock(paths.home / "gateway.lock")
    mutate = args.release is not None or args.terminate is not None
    if mutate:
        lock.acquire()
    try:
        async with await SqliteDatabase.open(paths.database_path) as database:
            repository = GatewayRepository(database)
            await repository.initialize()
            if not mutate:
                return await GatewayRecovery(repository).inspect_all()
            owner = await repository.acquire_owner(uuid4().hex, process_lock=lock)
            try:
                recovery = GatewayRecovery(repository, owner)
                if args.terminate is not None:
                    return [await recovery.terminate(args.terminate)]
                return [await recovery.release(args.release, note=args.note)]
            finally:
                await repository.release_owner(owner)
    finally:
        lock.close()


def recovery_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run gateway recover")
    parser.add_argument("--state-dir", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--release", metavar="RUN_ID")
    action.add_argument("--terminate", metavar="RUN_ID")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.release is not None and not args.note.strip():
        parser.error("--release requires --note describing the external-effect review")
    try:
        print(json.dumps(asyncio.run(_recover(args)), ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f"Recovery failed: {exc}") from exc
    return 0


__all__ = ["main", "run_gateway"]


if __name__ == "__main__":
    raise SystemExit(main())
