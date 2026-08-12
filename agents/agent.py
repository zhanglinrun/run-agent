"""Run Agent runtime: OpenAI-compatible chat + tool loop (C01–C07)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .mcp_client import McpManager
from .memory import (
    MemoryPrefetch,
    format_memories_for_injection,
    start_memory_prefetch,
)
from .prompt import build_system_prompt
from .session import save_folded_session_memory, save_session
from .session_memory import (
    FOLD_SESSION_MEMORY_SYSTEM,
    build_folding_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    parse_folded_memory,
)
from .skills import execute_skill, format_retrieved_skill_context
from .tools import TOOL_DEFINITIONS, check_permission, execute_tool, to_openai_tools
from .ui import (
    print_assistant_text,
    print_error,
    print_info,
    print_tool_call,
    print_tool_result,
    print_warning,
)

ConfirmFn = Callable[[str], Awaitable[bool]]
PlanApprovalFn = Callable[[str], Awaitable[dict[str, Any]]]

MODEL_CONTEXT: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "deepseek-chat": 200_000,
    "deepseek-v4-flash": 128_000,
}

SNIP_THRESHOLD = 0.60
AUTO_COMPACT_THRESHOLD = 0.70
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep", "list_files", "bash"}
MICROCOMPACT_IDLE_S = 5 * 60
KEEP_RECENT_RESULTS = 3
LARGE_RESULT_THRESHOLD = 30 * 1024


def _get_context_window(model: str) -> int:
    env = (os.environ.get("CONTEXT_WINDOW") or "").strip()
    if env.isdigit():
        return int(env)
    return MODEL_CONTEXT.get(model, 128_000)


class Agent:
    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        max_turns: int = 20,
        reasoning_effort: str | None = None,
    ) -> None:
        load_dotenv(override=False)

        self.permission_mode = permission_mode
        self.model = model or os.environ.get("MODEL") or "deepseek-v4-flash"
        self.max_turns = max_turns
        self.reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else (os.environ.get("REASONING_EFFORT") or "").strip() or None
        )

        key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("APIKEY")
        base = api_base or os.environ.get("OPENAI_BASE_URL") or os.environ.get("API")
        if not key:
            raise RuntimeError("OPENAI_API_KEY (or APIKEY) is required in .env")

        client_kwargs: dict[str, Any] = {"api_key": key}
        if base:
            client_kwargs["base_url"] = base
        self.client = AsyncOpenAI(**client_kwargs)

        self.session_id = uuid.uuid4().hex[:8]
        self._base_system_prompt = build_system_prompt()
        self._pre_plan_mode: str | None = None
        self._plan_file_path: str | None = None
        self._plan_approval_fn: PlanApprovalFn | None = None

        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._base_system_prompt},
        ]
        self.openai_tools = to_openai_tools(TOOL_DEFINITIONS)

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.effective_window = _get_context_window(self.model)
        self.last_api_call_time: float = 0.0
        self._confirm_fn: ConfirmFn | None = None
        self._confirmed: set[str] = set()
        self._aborted = False
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0

        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._context_cleared = False
        self._context_break = False
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        if self.permission_mode == "plan":
            self._enter_plan_mode(announce=False)
        else:
            self._sync_system_prompt()

    def set_confirm_fn(self, fn: ConfirmFn) -> None:
        self._confirm_fn = fn

    def set_plan_approval_fn(self, fn: PlanApprovalFn) -> None:
        self._plan_approval_fn = fn

    def abort(self) -> None:
        self._aborted = True

    def clear_history(self) -> None:
        system = self.messages[0]["content"] if self.messages else self._base_system_prompt
        if self.permission_mode == "plan" and self._plan_file_path:
            system = self._base_system_prompt + self._build_plan_mode_prompt(self._plan_file_path)
        self.messages = [{"role": "system", "content": system}]
        self._confirmed.clear()
        self._already_surfaced_memories.clear()
        self._session_memory_bytes = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        self._sync_system_prompt()
        self._auto_save()

    def show_cost(self) -> None:
        util = (
            self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        )
        print_info(
            f"tokens in={self.total_input_tokens} out={self.total_output_tokens} "
            f"ctx={util:.0%} folds={self._fold_count} (session {self.session_id})"
        )

    def restore_session(self, data: dict[str, Any]) -> None:
        """Load messages/tokens from a saved session.

        Does NOT override permission_mode so CLI flags like --plan --resume still apply.
        """
        if data.get("id"):
            self.session_id = str(data["id"])
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs:
            self.messages = list(msgs)
        if data.get("model"):
            self.model = str(data["model"])
            self.effective_window = _get_context_window(self.model)
        tokens = data.get("tokens") or {}
        self.total_input_tokens = int(tokens.get("input") or 0)
        self.total_output_tokens = int(tokens.get("output") or 0)
        if isinstance(data.get("foldedSessionMemories"), list):
            self._folded_session_memories = list(data["foldedSessionMemories"])
            self._fold_count = len(self._folded_session_memories)
        self._confirmed.clear()
        if self.permission_mode == "plan" and not self._plan_file_path:
            self._enter_plan_mode(announce=False)
        self._sync_system_prompt()
        print_info(f"Session restored ({len(self.messages)} messages, id={self.session_id}).")

    def toggle_plan_mode(self) -> str:
        if self.permission_mode == "plan":
            restored = self._pre_plan_mode or "default"
            self.permission_mode = restored
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._sync_system_prompt()
            msg = f"Exited plan mode -> {self.permission_mode}"
            print_info(msg)
            return msg

        self._pre_plan_mode = self.permission_mode
        self._enter_plan_mode(announce=True)
        return f"Entered plan mode. Plan file: {self._plan_file_path}"

    def _generate_plan_file_path(self) -> str:
        d = Path.cwd() / ".run" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str((d / f"plan-{self.session_id}.md").resolve())

    def _build_plan_mode_prompt(self, plan_file_path: str) -> str:
        return (
            "\n\n# Plan Mode Active\n\n"
            "Plan mode is active. You MUST NOT edit project files (except the plan file below),\n"
            "run non-readonly tools, or change the system.\n\n"
            f"## Plan File: {plan_file_path}\n"
            "Write your plan incrementally to this file using write_file or edit_file.\n"
            "This is the ONLY file you are allowed to edit.\n\n"
            "## Workflow\n"
            "1. Explore: use read_file / list_files / grep to understand the codebase.\n"
            "2. Design: outline steps, risks, and files you will touch.\n"
            "3. Write Plan: structured markdown in the plan file (goal / steps / files / risks).\n"
            "4. Exit: call exit_plan_mode when ready for user review.\n\n"
            "IMPORTANT: When the plan is complete, you MUST call exit_plan_mode.\n"
            "Do NOT ask the user to approve in plain text — exit_plan_mode handles approval.\n"
        )

    def _build_fold_guidance_section(self) -> str:
        utilization = (
            self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        )
        last_fold = (
            "never"
            if not self._fold_last_time
            else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        )
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, "
            "or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather "
            "than folding again.\n"
        )

    def _enter_plan_mode(self, *, announce: bool) -> None:
        self.permission_mode = "plan"
        if self._pre_plan_mode is None:
            self._pre_plan_mode = "default"
        self._plan_file_path = self._generate_plan_file_path()
        self._sync_system_prompt()
        if announce:
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")

    def _sync_system_prompt(self) -> None:
        content = self._base_system_prompt
        if self.permission_mode == "plan" and self._plan_file_path:
            content = self._base_system_prompt + self._build_plan_mode_prompt(self._plan_file_path)
        content += self._build_fold_guidance_section()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = content
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    def _read_plan_content(self) -> str:
        if not self._plan_file_path:
            return "(No plan file path)"
        path = Path(self._plan_file_path)
        if not path.exists():
            return "(No plan file found)"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"(Failed to read plan file: {e})"

    async def _execute_plan_mode_tool(self, name: str) -> str:
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return f"Already in plan mode.\nPlan file: {self._plan_file_path}"
            self._pre_plan_mode = self.permission_mode
            self._enter_plan_mode(announce=True)
            return (
                "Entered plan mode. You are now in read-only mode.\n\n"
                f"Your plan file: {self._plan_file_path}\n"
                "Write your plan to this file. This is the only file you can edit.\n\n"
                "When your plan is complete, call exit_plan_mode."
            )

        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."

            plan_content = self._read_plan_content()

            if self._plan_approval_fn is None:
                self.permission_mode = self._pre_plan_mode or "default"
                self._pre_plan_mode = None
                saved = self._plan_file_path
                self._plan_file_path = None
                self._sync_system_prompt()
                print_info(f"Exited plan mode -> {self.permission_mode}")
                return (
                    f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n"
                    f"Plan file: {saved}\n\n## Your Plan:\n{plan_content}"
                )

            result = await self._plan_approval_fn(plan_content)
            choice = result.get("choice", "manual-execute")

            if choice == "keep-planning":
                feedback = result.get("feedback") or "Please revise the plan."
                return (
                    "User rejected the plan and wants to keep planning.\n\n"
                    f"User feedback: {feedback}\n\n"
                    "Please revise your plan based on this feedback. "
                    "When done, call exit_plan_mode again."
                )

            if choice in {"clear-and-execute", "execute"}:
                target = "acceptEdits"
            else:
                target = self._pre_plan_mode or "default"

            saved = self._plan_file_path
            self.permission_mode = target
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._sync_system_prompt()

            cleared = ""
            if choice == "clear-and-execute":
                self.messages = [{"role": "system", "content": self.messages[0]["content"]}]
                self._confirmed.clear()
                cleared = " Context was cleared."

            print_info(f"Plan approved. Executing in {target} mode.")
            return (
                f"User approved the plan. Permission mode: {target}.{cleared}\n\n"
                f"Plan file: {saved}\n\n## Approved Plan:\n{plan_content}\n\n"
                "Proceed with implementation."
            )

        return f"Unknown plan mode tool: {name}"

    def _build_side_query(self, *, max_tokens: int = 256):
        """Lightweight no-tools completion for memory recall / folding."""
        client = self.client
        model = self.model

        async def _sq(system: str, user_message: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=max(1, int(max_tokens)),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            if not resp.choices:
                return ""
            return resp.choices[0].message.content or ""

        return _sq

    def _consume_memory_prefetch(self, memory_prefetch: MemoryPrefetch | None) -> None:
        if not memory_prefetch or not memory_prefetch.settled or memory_prefetch.consumed:
            return
        memory_prefetch.consumed = True
        try:
            memories = memory_prefetch.task.result()
            if not memories:
                return
            injection_text = format_memories_for_injection(memories)
            last = self.messages[-1] if self.messages else None
            if last and last.get("role") == "user":
                last["content"] = (last.get("content") or "") + "\n\n" + injection_text
            else:
                self.messages.append({"role": "user", "content": injection_text})
            for m in memories:
                self._already_surfaced_memories.add(m.path)
                self._session_memory_bytes += m.size
        except Exception:
            pass

    def _augment_user_message_with_skill_context(
        self, user_message: str
    ) -> tuple[str, dict[str, Any] | None]:
        try:
            context, top_ref = format_retrieved_skill_context(user_message, limit=3)
        except Exception:
            return user_message, None
        if not context.strip():
            return user_message, top_ref
        return f"{user_message}\n\n{context}", top_ref

    async def _execute_skill_tool(self, inp: dict) -> str:
        skill_name = str(inp.get("skill_name") or "").strip()
        result = execute_skill(skill_name, inp.get("args", ""))
        if not result:
            return f"Unknown skill: {skill_name}"
        if result.get("context") == "fork":
            return (
                f'Skill "{skill_name}" requests fork context, which is not enabled yet (C08). '
                "Use an inline skill or wait for sub-agent support."
            )
        return f'[Skill "{skill_name}" activated]\n\n{result["prompt"]}'

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    def _looks_like_tool_failure(self, result: str) -> bool:
        low = (result or "").lower()
        return low.startswith("error") or "action denied" in low or "user denied" in low

    async def compact(self) -> None:
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * AUTO_COMPACT_THRESHOLD:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation(trigger="auto")

    async def _compact_conversation(self, *, trigger: str = "manual") -> bool:
        compacted = await self._compact_openai(trigger=trigger)
        if compacted:
            print_info("Conversation compacted.")
        return compacted

    async def _compact_openai(self, *, trigger: str) -> bool:
        if len(self.messages) < 4:
            return False
        system_msg = self.messages[0]
        transcript = build_openai_transcript(self.messages)
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        self.messages = [
            system_msg,
            {"role": "user", "content": format_folded_memory(memory)},
        ]
        self.last_input_token_count = 0
        self._sync_system_prompt()
        self._auto_save()
        return True

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any]:
        side_query = self._build_side_query(max_tokens=6000)
        if side_query is None:
            return fallback_folded_memory(transcript)
        try:
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception:
            return fallback_folded_memory(transcript)

    def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "session_id": self.session_id,
            **memory,
        }
        self._folded_session_memories.append(record)
        try:
            save_folded_session_memory(self.session_id, record)
        except Exception:
            pass

    def _run_compression_pipeline(self) -> None:
        self._budget_tool_results_openai()
        self._snip_stale_results_openai()
        self._microcompact_openai()

    def _budget_tool_results_openai(self) -> None:
        utilization = (
            self.last_input_token_count / self.effective_window if self.effective_window else 0
        )
        if utilization < 0.5:
            return
        budget = 15_000 if utilization > 0.7 else 30_000
        for msg in self.messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                content = msg["content"]
                if len(content) > budget:
                    keep = (budget - 80) // 2
                    msg["content"] = (
                        content[:keep]
                        + f"\n\n[... budgeted: {len(content) - keep * 2} chars truncated ...]\n\n"
                        + content[-keep:]
                    )

    def _snip_stale_results_openai(self) -> None:
        utilization = (
            self.last_input_token_count / self.effective_window if self.effective_window else 0
        )
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs: list[int] = []
        for i, msg in enumerate(self.messages):
            if (
                msg.get("role") == "tool"
                and isinstance(msg.get("content"), str)
                and msg["content"] != SNIP_PLACEHOLDER
            ):
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self.messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs: list[int] = []
        for i, msg in enumerate(self.messages):
            if (
                msg.get("role") == "tool"
                and isinstance(msg.get("content"), str)
                and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]")
            ):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self.messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        raw = result.encode("utf-8", errors="replace")
        if len(raw) <= LARGE_RESULT_THRESHOLD:
            return result
        d = Path.home() / ".run" / "tool-results"
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
        filepath = d / filename
        filepath.write_text(result, encoding="utf-8")
        lines = result.split("\n")
        preview = "\n".join(lines[:200])
        size_kb = len(raw) / 1024
        return (
            f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
            f"Full output saved to {filepath}. "
            f"You can use read_file to see the full result.]\n\n"
            f"Preview (first 200 lines):\n{preview}"
        )

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            self._record_tool_outcome("compact_context", False)
            return (
                "No context compaction was performed because there is not enough "
                "conversation history yet."
            )
        self._record_tool_outcome("compact_context", True)
        self._context_cleared = True
        self._context_break = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )

    async def ensure_mcp(self) -> None:
        """Lazy-connect MCP servers once; append discovered tools to openai_tools."""
        if self._mcp_initialized:
            return
        self._mcp_initialized = True
        try:
            await self._mcp_manager.load_and_connect()
            mcp_defs = self._mcp_manager.get_tool_definitions()
            if mcp_defs:
                self.openai_tools = self.openai_tools + to_openai_tools(mcp_defs)
        except Exception as e:
            print_error(f"MCP init failed: {e}")

    def mcp_status(self) -> str:
        return self._mcp_manager.format_status()

    async def disconnect_mcp(self) -> None:
        await self._mcp_manager.disconnect_all()
        self._mcp_initialized = False

    async def _execute_tool(self, name: str, inp: dict) -> str:
        if self._mcp_manager.is_mcp_tool(name):
            try:
                return await self._mcp_manager.call_tool(name, inp)
            except Exception as e:
                return f"Error: {e}"
        return await execute_tool(name, inp)

    async def chat(self, user_message: str) -> None:
        self._aborted = False
        await self.ensure_mcp()
        original_user_message = user_message
        augmented, _ = self._augment_user_message_with_skill_context(original_user_message)
        self.messages.append({"role": "user", "content": augmented})

        memory_prefetch: MemoryPrefetch | None = None
        sq = self._build_side_query()
        if sq:
            memory_prefetch = start_memory_prefetch(
                original_user_message,
                sq,
                self._already_surfaced_memories,
                self._session_memory_bytes,
            )

        turns = 0
        while True:
            if self._aborted:
                break
            if turns >= self.max_turns:
                print_warning(f"Stopped: reached max_turns={self.max_turns}")
                break

            self._run_compression_pipeline()
            self._consume_memory_prefetch(memory_prefetch)
            self._sync_system_prompt()

            try:
                message, usage = await self._call_model()
            except Exception as e:
                print_error(f"model call failed: {e}")
                break

            self.last_api_call_time = time.time()
            if usage:
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                self.total_input_tokens += prompt_tokens
                self.total_output_tokens += int(usage.get("completion_tokens") or 0)
                self.last_input_token_count = prompt_tokens

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content"),
            }
            tool_calls = message.get("tool_calls")
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                text = (message.get("content") or "").strip()
                if text:
                    print_assistant_text(text + "\n")
                break

            turns += 1
            for tc in tool_calls:
                if self._aborted:
                    break
                await self._handle_tool_call(tc)
                if self._context_break:
                    self._context_break = False
                    break

            self._sync_system_prompt()
            await self._check_and_compact()
            self._auto_save()

        self._auto_save()

    async def _call_model(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "tools": self.openai_tools,
        }
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as first_err:
            if self.reasoning_effort and "extra_body" in kwargs:
                kwargs.pop("extra_body", None)
                print_warning(f"retry without reasoning_effort: {first_err}")
                resp = await self.client.chat.completions.create(**kwargs)
            else:
                raise
        choice = resp.choices[0]
        message = choice.message.model_dump(exclude_none=True)
        usage = resp.usage.model_dump() if resp.usage else None
        return message, usage

    async def _handle_tool_call(self, tc: dict[str, Any]) -> None:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        try:
            inp = json.loads(fn.get("arguments") or "{}")
            if not isinstance(inp, dict):
                inp = {}
        except json.JSONDecodeError:
            inp = {}

        print_tool_call(name, inp)

        if name in {"enter_plan_mode", "exit_plan_mode"}:
            result = await self._execute_plan_mode_tool(name)
            print_tool_result(name, result)
            self._record_tool_outcome(name, not self._looks_like_tool_failure(result))
            self.messages.append(
                {"role": "tool", "tool_call_id": tc_id, "content": result}
            )
            return

        if name == "skill":
            result = await self._execute_skill_tool(inp)
            print_tool_result(name, result)
            self._record_tool_outcome(name, not self._looks_like_tool_failure(result))
            self.messages.append(
                {"role": "tool", "tool_call_id": tc_id, "content": result}
            )
            return

        if name == "compact_context":
            result = await self._execute_compact_context_tool(inp)
            print_tool_result(name, result)
            if self._context_cleared:
                self._context_cleared = False
                # History was replaced; tool_call pairing is gone — append as user note.
                self.messages.append({"role": "user", "content": result})
            else:
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc_id, "content": result}
                )
            return

        perm = check_permission(
            self.permission_mode,
            name,
            inp,
            plan_file_path=self._plan_file_path,
        )

        if perm["action"] == "deny":
            print_info(f"Denied: {perm.get('message', '')}")
            result = f"Action denied: {perm.get('message', '')}"
            self._record_tool_outcome(name, False)
        elif perm["action"] == "confirm":
            ok = await self._confirm(perm.get("message") or name)
            if not ok:
                result = "User denied this action."
                self._record_tool_outcome(name, False)
            else:
                result = await self._execute_tool(name, inp)
                result = self._persist_large_result(name, result)
                print_tool_result(name, result)
                self._record_tool_outcome(name, not self._looks_like_tool_failure(result))
        else:
            result = await self._execute_tool(name, inp)
            result = self._persist_large_result(name, result)
            print_tool_result(name, result)
            self._record_tool_outcome(name, not self._looks_like_tool_failure(result))

        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            }
        )

    async def _confirm(self, message: str) -> bool:
        if message in self._confirmed:
            return True
        if self._confirm_fn is None:
            print_warning(f"Needs confirm but no confirm_fn: {message}")
            return False
        ok = await self._confirm_fn(message)
        if ok:
            self._confirmed.add(message)
        return ok

    def _auto_save(self) -> None:
        try:
            save_session(
                self.session_id,
                {
                    "id": self.session_id,
                    "model": self.model,
                    "permission_mode": self.permission_mode,
                    "plan_file_path": self._plan_file_path,
                    "messages": self.messages,
                    "foldedSessionMemories": self._folded_session_memories,
                    "updated_at": time.time(),
                    "tokens": {
                        "input": self.total_input_tokens,
                        "output": self.total_output_tokens,
                    },
                },
            )
        except Exception:
            pass
