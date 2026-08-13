"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from dotenv import find_dotenv, load_dotenv

from .agent import Agent
from .api_config import resolve_api_config
from .memory import list_memories
from .online_skill_eval import format_online_skill_eval_async
from .session import get_latest_session_id, list_sessions, load_session
from .skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
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
    print_skill_entries,
    print_user_prompt,
    print_warning,
    print_welcome,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run-agent",
        description="Run Agent — a local coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI or Anthropic-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--session", default=None, help="Resume a specific session id")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
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


def _load_env_file() -> None:
    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


def _print_help() -> None:
    print(
        """
Usage: python -m agents.main [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: MODEL env, else deepseek-chat)
  --api-base URL      Override API base URL from CLI or .env
  --resume            Resume the last session
  --session ID        Resume a specific session id
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --help, -h          Show this help

REPL commands:
  /help               Show this help
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /sessions           List recent sessions
  /resume             Pick a session interactively (or load latest if only one)
  /resume <id|n>      Resume by session id or list number
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /mcp                List connected MCP servers and tools
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate online skill evolution quality
  /extract_now        Extract the current pending online skill window: /extract_now [hint]
  /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skill-create       Create a skill: /skill-create <name> | <description> | <when-to-use> | <instructions>
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")
  exit                Quit

Notes:
  Runtime dir is .run/; env prefix is RUN_. Dual protocol: OPENAI_* by default;
  ANTHROPIC_* or a base URL containing /anthropic uses Messages API.
  Skill evolution: set RUN_AUTO_SKILL_EVOLUTION=0 to disable.
""".strip()
    )


def _print_sessions(items: list[dict]) -> None:
    if not items:
        print_info("No previous sessions found.")
        return
    print_info(f"{len(items)} session(s) under ~/.run-agent/sessions:")
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
    exact = [x for x in items if x["id"] == selector]
    if exact:
        return exact[0]["id"]
    prefixed = [x for x in items if x["id"].startswith(selector)]
    if len(prefixed) == 1:
        return prefixed[0]["id"]
    if len(prefixed) > 1:
        print_warning("Ambiguous id prefix " + selector + "; matches: " + ", ".join(x["id"] for x in prefixed))
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
    agent.restore_session(
        {
            "anthropicMessages": data.get("anthropicMessages"),
            "openaiMessages": data.get("openaiMessages") or data.get("messages"),
            "foldedSessionMemories": data.get("foldedSessionMemories"),
        }
    )


def _resume_interactive(agent: Agent) -> None:
    items = list_sessions()
    if not items:
        print_info("No previous sessions found.")
        return
    if len(items) == 1:
        _resume_session(agent, items[0]["id"])
        return
    _print_sessions(items)
    print_info("Enter number or session id (empty = latest):")
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


async def run_repl(agent: Agent) -> None:
    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            if choice == "2":
                return {"choice": "execute"}
            if choice == "3":
                return {"choice": "manual-execute"}
            if choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(_sig, _frame):
        nonlocal sigint_count
        if agent._aborted is False and agent._output_buffer is not None:
            agent.abort()
            print_interrupted()
            sigint_count = 0
            print_user_prompt()
            return
        sigint_count += 1
        if sigint_count >= 2:
            print_goodbye()
            sys.exit(0)
        print_warning("Press Ctrl+C again to exit.")
        print_user_prompt()

    try:
        signal.signal(signal.SIGINT, handle_sigint)
    except Exception:
        pass

    print_welcome()

    try:
        while True:
            print_user_prompt()
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                print_goodbye()
                break

            inp = line.strip()
            sigint_count = 0
            if not inp:
                continue
            if inp in ("exit", "quit", "/exit"):
                print_goodbye()
                break
            if inp in {"/help", "help"}:
                _print_help()
                continue
            if inp == "/clear":
                agent.clear_history()
                continue
            if inp == "/plan":
                agent.toggle_plan_mode()
                continue
            if inp == "/cost":
                agent.show_cost()
                continue
            if inp == "/sessions":
                _print_sessions(list_sessions())
                continue
            if inp == "/resume":
                _resume_interactive(agent)
                continue
            if inp.startswith("/resume "):
                _resume_session(agent, inp[len("/resume "):].strip())
                continue
            if inp == "/compact":
                try:
                    await agent.compact()
                except Exception as e:
                    print_error(str(e))
                continue
            if inp == "/memory":
                memories = list_memories()
                if not memories:
                    print_info("No memories saved yet.")
                else:
                    print_memory_entries(memories)
                continue
            if inp == "/skills":
                reset_skill_cache()
                skills = discover_skills()
                if not skills:
                    print_info("No skills found. Add skills to .run/skills/<name>/SKILL.md")
                else:
                    print_skill_entries(skills)
                continue
            if inp == "/mcp":
                await agent.ensure_mcp()
                print_info(agent.mcp_status())
                continue
            if inp in {"/skill-stats", "/skills-stats"}:
                print_info(skill_stats())
                continue
            if inp in {"/skill-eval", "/skills-eval"}:
                side_query = agent._build_side_query(max_tokens=2400)
                print_info(await format_online_skill_eval_async(side_query=side_query))
                continue
            if inp == "/extract_now" or inp.startswith("/extract_now "):
                hint = inp[len("/extract_now"):].strip()
                result = await agent.extract_now(hint)
                if result.get("ok"):
                    print_info("Ran online skill extraction for the current pending window.")
                else:
                    print_error(str(result.get("error") or result))
                continue
            if inp.startswith("/skill-feedback "):
                _, rest = inp.split(" ", 1)
                parts = rest.strip().split(" ", 2)
                if len(parts) < 2:
                    print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
                    continue
                note = parts[2] if len(parts) > 2 else ""
                record_feedback(parts[0], parts[1], note)
                print_info(f"Recorded feedback for skill: {parts[0]}")
                continue
            if inp.startswith("/skill-evolve "):
                _, rest = inp.split(" ", 1)
                parts = rest.strip().split(" ", 1)
                if len(parts) < 2:
                    print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                    continue
                result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
                if result.get("ok"):
                    print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
                else:
                    print_error(str(result.get("error") or result))
                continue
            if inp.startswith("/skill-create "):
                _, rest = inp.split(" ", 1)
                parts = [part.strip() for part in rest.split("|", 3)]
                if len(parts) < 4 or not all(parts[:4]):
                    print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
                    continue
                result = create_skill(
                    name=parts[0],
                    description=parts[1],
                    when_to_use=parts[2],
                    instructions=parts[3],
                    target="project",
                    context="inline",
                    user_invocable=False,
                    evidence="Manual REPL skill creation",
                )
                if result.get("ok"):
                    print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
                else:
                    print_error(str(result.get("error") or result))
                continue

            if inp.startswith("/"):
                space_idx = inp.find(" ")
                cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
                cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
                skill = get_skill_by_name(cmd_name)
                if skill and skill.user_invocable:
                    print_info(f"Invoking skill: {skill.name}")
                    try:
                        if skill.context == "fork":
                            await agent.chat(
                                f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}'
                            )
                        else:
                            result = execute_skill(skill.name, cmd_args)
                            if not result:
                                print_error(f"Unknown skill: {skill.name}")
                                continue
                            await agent.chat(result["prompt"])
                    except Exception as e:
                        if "abort" not in str(e).lower():
                            print_error(str(e))
                    continue

            try:
                await agent.chat(inp)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))
    finally:
        await agent.drain_background_skill_tasks()
        await agent.disconnect_mcp()


async def run_one_shot(agent: Agent, prompt: str) -> None:
    try:
        await agent.chat(prompt)
        await agent.drain_background_skill_tasks()
    finally:
        await agent.disconnect_mcp()


def main() -> None:
    args = parse_args()
    _load_env_file()

    if args.help:
        _print_help()
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    model = args.model or os.environ.get("MODEL") or "deepseek-chat"
    resolved_api_base, resolved_api_key, resolved_use_openai = resolve_api_config(
        cli_api_base=args.api_base
    )

    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
        use_openai=resolved_use_openai,
    )

    if args.session:
        _resume_session(agent, args.session)
    elif args.resume:
        session_id = get_latest_session_id()
        if session_id:
            _resume_session(agent, session_id)
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None
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
