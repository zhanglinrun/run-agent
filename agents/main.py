"""CLI entry: REPL and one-shot prompts."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from .agent import Agent
from .ui import (
    print_error,
    print_goodbye,
    print_info,
    print_interrupted,
    print_user_prompt,
    print_welcome,
    print_warning,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run-agent",
        description="Run Agent — local coding agent CLI",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip confirmations")
    parser.add_argument("--plan", action="store_true", help="Read-only plan mode")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations")
    parser.add_argument("--model", "-m", default=None, help="Model name")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible base URL")
    parser.add_argument("--max-turns", type=int, default=20, help="Max tool loop turns")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def _print_help() -> None:
    print(
        """
Usage: python -m agents.main [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts
  --plan              Read-only plan mode
  --accept-edits      Auto-approve file edits
  --dont-ask          Auto-deny confirmations
  --model, -m         Model name (or MODEL in .env)
  --api-base URL      OpenAI-compatible base URL
  --max-turns N       Max agentic turns (default 20)
  --help, -h          Show help

REPL:
  /help               Show help
  /clear              Clear history
  /cost               Show token usage
  /exit               Quit
""".strip()
    )


async def _confirm_interactive(message: str) -> bool:
    print_warning(f"Confirm dangerous action:\n  {message}")
    print_user_prompt()
    try:
        ans = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}


async def run_repl(agent: Agent) -> None:
    print_welcome()
    while True:
        try:
            print_user_prompt()
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            print_interrupted()
            print_goodbye()
            break

        if not line:
            continue
        if line in {"/exit", "exit", "quit"}:
            print_goodbye()
            break
        if line in {"/help", "help"}:
            _print_help()
            continue
        if line == "/clear":
            agent.clear_history()
            print_info("history cleared")
            continue
        if line == "/cost":
            agent.show_cost()
            continue

        try:
            await agent.chat(line)
        except KeyboardInterrupt:
            agent.abort()
            print_interrupted()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    await agent.chat(prompt)


def main() -> None:
    load_dotenv(override=False)
    args = parse_args()
    if args.help:
        _print_help()
        sys.exit(0)

    mode = _resolve_permission_mode(args)
    try:
        agent = Agent(
            permission_mode=mode,
            model=args.model,
            api_base=args.api_base,
            max_turns=args.max_turns,
        )
    except Exception as e:
        print_error(str(e))
        sys.exit(1)

    agent.set_confirm_fn(_confirm_interactive)

    prompt = " ".join(args.prompt).strip()
    try:
        if prompt:
            asyncio.run(run_one_shot(agent, prompt))
        else:
            asyncio.run(run_repl(agent))
    except KeyboardInterrupt:
        print_interrupted()
        sys.exit(130)


if __name__ == "__main__":
    main()
