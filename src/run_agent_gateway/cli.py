"""Command-line entry for the Feishu gateway."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from run_agent_coding.application import ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import load_provider_settings
from run_agent_coding.thinking import normalize_thinking_level
from run_agent_extensions import resolve_extension_path
from run_agent_gateway.config import load_gateway_config
from run_agent_gateway.platforms.feishu import FeishuAdapter
from run_agent_gateway.platforms.feishu_lock import lock_path_for
from run_agent_gateway.run import GatewayRunner


async def run_gateway(args: argparse.Namespace) -> None:
    cwd = args.cwd.resolve()
    load_dotenv(cwd / ".env", override=False)
    config = load_gateway_config()
    paths = RunAgentPaths(home=args.state_dir.resolve()) if args.state_dir else RunAgentPaths()
    settings = load_provider_settings(paths)
    provider = settings.get_provider(args.provider)
    thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    options = ApplicationOptions(
        cwd=cwd,
        paths=paths,
        provider_name=provider.name,
        model=args.model or os.environ.get("MODEL") or provider.default_model,
        thinking=normalize_thinking_level(thinking) if thinking else None,
        extension_paths=tuple(resolve_extension_path(item) for item in args.extension),
        extensions_enabled=not args.no_extensions,
        refresh_resources=args.refresh_resources,
        project_extensions_enabled=args.project_extensions,
        trust_default="always" if args.trust_project else "never",
    )
    adapter = FeishuAdapter(
        config.feishu,
        group_sessions_per_user=config.group_sessions_per_user,
        thread_sessions_per_user=config.thread_sessions_per_user,
        busy_input_mode=config.busy_input_mode,
        busy_queue_max_pending=config.busy_queue_max_pending,
        dedup_path=paths.home / "gateway" / "feishu-dedup.json",
        lock_path=lock_path_for(paths.home / "gateway" / "locks", config.feishu.app_id),
    )
    runner = GatewayRunner(config, options, adapter, settings=settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)
    await runner.run_until(stop)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run gateway",
        description=(
            "Serve Run Agent over Feishu. Credentials and policy come from the environment "
            "(FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_ALLOWED_USERS, ...)."
        ),
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="project directory")
    parser.add_argument("--state-dir", type=Path, help="application state directory")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        help="session extension name or path (repeatable)",
    )
    parser.add_argument("--project-extensions", action="store_true")
    parser.add_argument(
        "--no-extensions",
        action="store_true",
        help="disable default and discovered extensions; explicit --extension paths still load",
    )
    parser.add_argument(
        "--refresh-resources",
        action="store_true",
        help="refresh resumed session resources, including the default experience extension",
    )
    parser.add_argument("--trust-project", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
