"""Command-line gateway host for trusted channel extensions."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from run_agent_coding.thinking import normalize_thinking_level
from run_agent_gateway.coding import CodingSessionTurnRunner
from run_agent_gateway.extensions import GatewayExtensionHost
from run_agent_gateway.gateway import AgentGateway
from run_agent_gateway.runtime import CodingSessionPool
from run_agent_gateway.scheduler import TurnScheduler


async def run_gateway(args: argparse.Namespace) -> None:
    cwd = args.cwd.resolve()
    load_dotenv(cwd / ".env", override=False)
    host = GatewayExtensionHost()
    adapters = host.load(args.extension)
    if not adapters:
        raise ValueError("gateway requires at least one registered adapter")
    thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    pool = CodingSessionPool(
        cwd=cwd,
        provider_name=args.provider,
        model=args.model or os.environ.get("MODEL"),
        thinking_level_override=(normalize_thinking_level(thinking) if thinking else None),
        extension_paths=tuple(path.resolve() for path in args.agent_extension),
        project_extensions_enabled=args.project_extensions,
        trust_default="always" if args.trust_project else "never",
    )
    scheduler = TurnScheduler(
        CodingSessionTurnRunner(pool.resolve),
        foreground_limit=args.foreground_limit,
        background_limit=args.background_limit,
        max_queued=args.max_queued,
    )
    gateway = AgentGateway(scheduler, adapters)
    try:
        await gateway.start()
        await gateway.wait_closed()
    finally:
        await gateway.shutdown(grace_period=args.grace_period)
        await pool.aclose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-agent-gateway",
        description="Run the session-aware Run Agent gateway with trusted adapter extensions.",
    )
    parser.add_argument("--extension", type=Path, action="append", default=[], required=True)
    parser.add_argument("--agent-extension", type=Path, action="append", default=[])
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument("--foreground-limit", type=int, default=4)
    parser.add_argument("--background-limit", type=int, default=1)
    parser.add_argument("--max-queued", type=int, default=256)
    parser.add_argument("--grace-period", type=float, default=5.0)
    parser.add_argument("--project-extensions", action="store_true")
    parser.add_argument("--trust-project", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(run_gateway(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Gateway failed: {exc}") from exc
    return 0


__all__ = ["main", "run_gateway"]


if __name__ == "__main__":
    raise SystemExit(main())
