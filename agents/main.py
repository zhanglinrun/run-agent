"""CLI entry: REPL and one-shot prompts (C02: flags + resume; C07: MCP)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from .agent import Agent
from .memory import list_memories
from .session import list_sessions, load_session
from .skills import (
    discover_skills,
    evolve_skill,
    reset_skill_cache,
    skill_stats,
)
from .ui import (
    print_error,
    print_goodbye,
    print_info,
    print_interrupted,
    print_memory_entries,
    print_plan_approval_options,
    print_plan_for_approval,
    print_skills,
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
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--session", default=None, help="Resume a specific session id")
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
  --plan              Read-only plan mode (explore + write plan file only)
  --accept-edits      Auto-approve file edits
  --dont-ask          Auto-deny confirmations
  --model, -m         Model name (or MODEL in .env)
  --api-base URL      OpenAI-compatible base URL
  --resume            Resume the last session under .run/sessions
  --session ID        Resume a specific session id
  --max-turns N       Max agentic turns (default 20)
  --help, -h          Show help

REPL:
  /help               Show help
  /clear              Clear history
  /cost               Show token usage
  /sessions           List recent sessions
  /resume             Pick a session interactively (or load latest if only one)
  /resume <id|n>      Resume by session id or list number
  /plan               Toggle plan mode (read-only planning)
  /memory             List long-term memories for this project
  /skills             List discovered skills
  /compact            Compact conversation into structured session memory
  /mcp                List connected MCP servers and tools
  /extract_now        Run online skill extraction on the pending window: /extract_now [hint]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skills-stats       Show skill evolution / usage stats
  /exit               Quit

Notes:
  Sub-agents: the model may call the `agent` tool (explore/plan/general).
  Skill evolution: after a turn, the next user message can trigger online add/merge
  (set RUN_AUTO_SKILL_EVOLUTION=0 to disable). Background writes auto-apply under -y / --accept-edits.
""".strip()
    )


def _print_sessions(items: list[dict]) -> None:
    if not items:
        print_info("No previous sessions found.")
        return
    print_info(f"{len(items)} session(s) under .run/sessions:")
    for i, item in enumerate(items, start=1):
        model = item.get("model") or "-"
        print(
            f"  {i:2d}. {item['id']}  "
            f"msgs={item['message_count']:<3}  "
            f"{item['updated_at_str']}  "
            f"[{model}]  "
            f"{item['preview']}"
        )


def _resolve_session_id(selector: str | None) -> str | None:
    """Resolve 'latest' / id / 1-based index to a session id."""
    items = list_sessions()
    if not items:
        return None
    if selector is None or selector in {"", "__latest__", "latest"}:
        return items[0]["id"]
    if selector.isdigit():
        idx = int(selector)
        if 1 <= idx <= len(items):
            return items[idx - 1]["id"]
        print_warning(f"Invalid index {selector}; use /sessions and pick 1..{len(items)}")
        return None
    # exact id or unique prefix
    exact = [x for x in items if x["id"] == selector]
    if exact:
        return exact[0]["id"]
    prefixed = [x for x in items if x["id"].startswith(selector)]
    if len(prefixed) == 1:
        return prefixed[0]["id"]
    if len(prefixed) > 1:
        print_warning(f"Ambiguous id prefix {selector!r}; matches: " + ", ".join(x["id"] for x in prefixed))
        return None
    print_warning(f"Session not found: {selector}")
    return None


def _resume_session(agent: Agent, selector: str | None = "__latest__") -> None:
    session_id = _resolve_session_id(selector)
    if not session_id:
        if selector in {None, "", "__latest__", "latest"}:
            print_info("No previous sessions found.")
        return
    data = load_session(session_id)
    if not data:
        print_info("No session found to resume.")
        return
    agent.restore_session(data)


def _resume_interactive(agent: Agent) -> None:
    items = list_sessions()
    if not items:
        print_info("No previous sessions found.")
        return
    if len(items) == 1:
        _resume_session(agent, items[0]["id"])
        return

    _print_sessions(items)
    print_info("Enter number or session id (empty = latest / cancel with Ctrl+C):")
    print_user_prompt()
    try:
        choice = input().strip()
    except (EOFError, KeyboardInterrupt):
        print_interrupted()
        return
    if not choice:
        _resume_session(agent, items[0]["id"])
        return
    _resume_session(agent, choice)


async def _confirm_interactive(message: str) -> bool:
    print_warning(f"Confirm dangerous action:\n  {message}")
    print_user_prompt()
    try:
        ans = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}


async def _plan_approval_fn(plan_content: str) -> dict:
    print_plan_for_approval(plan_content)
    print_plan_approval_options()
    while True:
        print_user_prompt()
        try:
            choice = input("Enter choice (1-4): ").strip()
        except (EOFError, KeyboardInterrupt):
            print_interrupted()
            return {"choice": "manual-execute"}
        if choice == "1":
            return {"choice": "clear-and-execute"}
        if choice == "2":
            return {"choice": "execute"}
        if choice == "3":
            return {"choice": "manual-execute"}
        if choice == "4":
            print_user_prompt()
            try:
                feedback = input("Feedback (what to change): ").strip()
            except (EOFError, KeyboardInterrupt):
                print_interrupted()
                return {"choice": "keep-planning", "feedback": None}
            return {"choice": "keep-planning", "feedback": feedback or None}
        print_warning("Invalid choice. Enter 1, 2, 3, or 4.")


async def run_one_shot(agent: Agent, prompt: str) -> None:
    try:
        await agent.chat(prompt)
    finally:
        await agent.disconnect_mcp()


async def run_repl(agent: Agent) -> None:
    print_welcome()
    try:
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
            if line == "/sessions":
                _print_sessions(list_sessions())
                continue
            if line == "/resume":
                _resume_interactive(agent)
                continue
            if line.startswith("/resume "):
                _resume_session(agent, line[len("/resume ") :].strip())
                continue
            if line == "/plan":
                agent.toggle_plan_mode()
                continue
            if line == "/memory":
                memories = list_memories()
                if not memories:
                    print_info("No memories saved yet.")
                else:
                    print_memory_entries(memories)
                continue
            if line == "/skills":
                reset_skill_cache()
                skills = discover_skills()
                if not skills:
                    print_info("No skills registered. Add .run/skills/<name>/SKILL.md")
                else:
                    print_skills(skills)
                continue
            if line == "/compact":
                await agent.compact()
                continue
            if line == "/mcp":
                await agent.ensure_mcp()
                print_info(agent.mcp_status())
                continue
            if line == "/extract_now" or line.startswith("/extract_now "):
                hint = ""
                if line.startswith("/extract_now "):
                    hint = line[len("/extract_now ") :].strip()
                result = await agent.extract_now(hint)
                if result.get("ok"):
                    print_info("Ran online skill extraction for the current pending window.")
                else:
                    print_error(str(result.get("error") or result))
                continue
            if line.startswith("/skill-evolve "):
                parts = line[len("/skill-evolve ") :].strip().split(None, 1)
                if len(parts) < 2:
                    print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                    continue
                result = evolve_skill(
                    parts[0], parts[1], rationale="Manual REPL evolution", target="active"
                )
                if result.get("ok"):
                    print_info(f"Evolved skill {result.get('skill')} -> v{result.get('version')}")
                else:
                    print_error(str(result.get("error") or result))
                continue
            if line in {"/skills-stats", "/skill-stats"}:
                print_info(skill_stats())
                continue

            try:
                await agent.chat(line)
            except KeyboardInterrupt:
                agent.abort()
                print_interrupted()
    finally:
        await agent.disconnect_mcp()


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
    agent.set_plan_approval_fn(_plan_approval_fn)
    if args.session:
        _resume_session(agent, args.session)
    elif args.resume:
        _resume_session(agent, "__latest__")

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
