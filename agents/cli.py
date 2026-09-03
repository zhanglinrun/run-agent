"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from dotenv import find_dotenv, load_dotenv

from .app import Agent
from .execution import SandboxSpec, prepare_workspace_for_container, scrub_workspace_credentials
from .providers.config import resolve_api_config
from .context.memory import list_memories
from .evolution.skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    get_skill_by_name,
    record_feedback,
    skill_stats,
)
from .runtime.ui import (
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


_REPL_HELP = """REPL commands:
  /help               Show this help
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /sessions           List recent sessions
  /resume [id|n]      Resume interactively, by session id, or list number
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /mcp                List connected MCP servers and tools
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate Skill candidates and replay evidence
  /skill-feedback     Record feedback for one Skill
  /skill-evolve       Record a durable lesson for one Skill
  /skill-create       Create a Skill from pipe-separated fields
  /<skill-name>       Invoke a user-visible Skill
  exit                Quit

Runtime data lives under .run/. Provider settings use OPENAI_* by default;
ANTHROPIC_* or a base URL containing /anthropic selects the Messages API."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-agent",
        description="Run Agent - a local coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_REPL_HELP,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    permissions = parser.add_mutually_exclusive_group()
    permissions.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    permissions.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    permissions.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    permissions.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--temperature", type=float, default=None, help="Sampling temperature (provider default if omitted)")
    parser.add_argument("--api-base", default=None, help="OpenAI or Anthropic-compatible API base URL")
    parser.add_argument("--extension", "-e", action="append", default=[], help="Load a trusted Python extension file or directory")
    parser.add_argument("--disable-extension", action="append", default=[], help="Disable one extension from the default profile")
    parser.add_argument("--no-default-extensions", action="store_true", help="Start without the built-in extension profile")
    parser.add_argument("--no-user-extensions", action="store_true", help="Do not load ~/.run/extensions")
    parser.add_argument("--trust-project-extensions", action="store_true", help="Execute Python extensions from .run/extensions")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--session", default=None, help="Resume a specific session id")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--sandbox", choices=["local", "docker"], default="local", help="Execution backend")
    parser.add_argument("--sandbox-image", default="run-agent-python-sandbox:latest", help="Docker sandbox image")
    parser.add_argument("--allow-host-shell", action="store_true", help="Expose unbounded host-local shell execution to the model")
    parser.add_argument("--memory-mb", type=int, default=2048, help="Docker memory limit")
    parser.add_argument("--cpus", type=float, default=2.0, help="Docker CPU limit")
    parser.add_argument("--pids-limit", type=int, default=256, help="Docker PID limit")
    parser.add_argument("--network", default="none", help="Docker network mode")
    return parser


def parse_args() -> argparse.Namespace:
    return _build_parser().parse_args()


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
    _build_parser().print_help()


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


def _resolve_session_id(agent: Agent, selector: str | None) -> str | None:
    items = agent.list_sessions()
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
    session_id = _resolve_session_id(agent, selector)
    if not session_id:
        if selector in {None, "", "__latest__", "latest"}:
            print_info("No previous sessions found.")
        return
    if not agent.resume(session_id):
        print_info("No session found to resume.")
    else:
        print_info(f"Resumed SQLite session: {session_id}")


def _resume_interactive(agent: Agent) -> None:
    items = agent.list_sessions()
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


def _initialize_sandbox_baseline(workspace: Path) -> None:
    commands = (
        ("git", "init", "-q"),
        ("git", "add", "-A", "-f"),
        ("git", "-c", "user.name=Run Agent Sandbox", "-c", "user.email=sandbox@run-agent.local",
         "commit", "-q", "-m", "sandbox baseline"),
    )
    for argv in commands:
        result = subprocess.run(
            list(argv), cwd=str(workspace), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, shell=False, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to initialize sandbox baseline: {' '.join(argv)}\n{result.stderr}")


async def _finalize_agent(agent: Agent, artifact_root: Path | None) -> None:
    result = agent.last_result
    artifact_dir: Path | None = None
    if artifact_root is not None:
        suffix = agent.session_id or "no-session"
        artifact_dir = artifact_root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{suffix}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        patch = result.patch if result is not None else ""
        (artifact_dir / "patch.diff").write_text(patch, encoding="utf-8")
        trace_path = result.trace_path if result is not None else None
        if trace_path and trace_path.exists():
            shutil.copyfile(trace_path, artifact_dir / "trace.jsonl")
        (artifact_dir / "verification.json").write_text(json.dumps({
            "status": result.verification.outcome if result and result.verification else "not_run",
            "report": result.verification.to_dict() if result and result.verification else None,
            "failure": result.failure.to_dict() if result and result.failure else None,
            "repair_attempts": len(result.correction_attempts) if result else 0,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sandbox = result.metadata.get("sandbox", {}) if result is not None else {}
        (artifact_dir / "sandbox.json").write_text(json.dumps({
            **(sandbox if isinstance(sandbox, dict) else {}),
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch else None,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print_info(f"Sandbox artifacts: {artifact_dir}")
    await agent.close()


async def run_repl(agent: Agent, artifact_root: Path | None = None) -> None:
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
                return {"choice": "keep-planning"}
            if choice == "1":
                return {"choice": "approve"}
            if choice == "2":
                return {"choice": "reject"}
            if choice == "3":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "revise", "feedback": feedback or None}
            if choice == "4":
                return {"choice": "cancel"}
            print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(_sig, _frame):
        nonlocal sigint_count
        if not agent.aborted and agent.is_running:
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
                try:
                    await agent.chat("/plan")
                except Exception as e:
                    print_error(str(e))
                continue
            if inp == "/cost":
                agent.show_cost()
                continue
            if inp == "/sessions":
                _print_sessions(agent.list_sessions())
                continue
            if inp == "/resume":
                _resume_interactive(agent)
                continue
            if inp.startswith("/resume "):
                _resume_session(agent, inp[len("/resume "):].strip())
                continue
            if inp == "/compact":
                try:
                    await agent.chat("/compact")
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
                skills = discover_skills()
                if not skills:
                    print_info("No skills found. Add skills to .run/skills/<name>/SKILL.md")
                else:
                    print_skill_entries(skills)
                continue
            if inp == "/mcp":
                try:
                    await agent.chat("/mcp")
                except Exception as e:
                    print_error(str(e))
                continue
            if inp == "/skill-stats":
                print_info(skill_stats())
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
                        await agent.chat(
                            f'Invoke the skill tool for "{skill.name}" with args: {cmd_args or "(none)"}. Follow the activated Skill exactly.'
                        )
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
        await _finalize_agent(agent, artifact_root)


async def run_one_shot(agent: Agent, prompt: str, artifact_root: Path | None = None) -> None:
    try:
        await agent.chat(prompt)
    finally:
        await _finalize_agent(agent, artifact_root)


def main() -> None:
    args = parse_args()
    _load_env_file()

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

    original_cwd = Path.cwd()
    sandbox_workspace = None
    sandbox_temp = None
    sandbox_spec = None
    if args.sandbox == "docker":
        source = Path.cwd().resolve()
        sandbox_temp = tempfile.TemporaryDirectory(prefix="run-agent-cli-")
        sandbox_workspace = Path(sandbox_temp.name) / "workspace"
        shutil.copytree(
            source,
            sandbox_workspace,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", ".run", "__pycache__", ".env", ".env.*",
                ".ssh", ".aws", ".azure", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
            ),
        )
        scrub_workspace_credentials(sandbox_workspace)
        _initialize_sandbox_baseline(sandbox_workspace)
        prepare_workspace_for_container(sandbox_workspace)
        sandbox_spec = SandboxSpec(
            workspace=sandbox_workspace,
            image=args.sandbox_image,
            network=args.network,
            memory_mb=args.memory_mb,
            cpus=args.cpus,
            pids_limit=args.pids_limit,
        )

    disabled_extensions = set(args.disable_extension)
    if args.sandbox == "docker":
        disabled_extensions.update({"memory", "skills", "skill-evolution", "mcp"})

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base,
        api_key=resolved_api_key,
        use_openai=resolved_use_openai,
        temperature=args.temperature,
        execution_backend=args.sandbox,
        sandbox_spec=sandbox_spec,
        workspace=sandbox_workspace or original_cwd,
        allow_host_shell=args.allow_host_shell,
        persist_session=args.sandbox != "docker",
        trust_project_extensions=args.trust_project_extensions,
        extension_paths=args.extension,
        disable_extensions=tuple(sorted(disabled_extensions)),
        use_default_extensions=not args.no_default_extensions,
        load_user_extensions=not args.no_user_extensions,
    )

    if args.session:
        _resume_session(agent, args.session)
    elif args.resume:
        sessions = agent.list_sessions(limit=1)
        session_id = sessions[0]["id"] if sessions else None
        if session_id:
            _resume_session(agent, session_id)
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None
    sandbox_artifact_root = original_cwd / ".run" / "sandbox-runs" if args.sandbox == "docker" else None
    try:
        if prompt:
            asyncio.run(run_one_shot(agent, prompt, sandbox_artifact_root))
        else:
            asyncio.run(run_repl(agent, sandbox_artifact_root))
    except KeyboardInterrupt:
        print_interrupted()
        sys.exit(130)
    finally:
        if sandbox_temp is not None:
            sandbox_temp.cleanup()


if __name__ == "__main__":
    main()
