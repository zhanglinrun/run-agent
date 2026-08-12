"""Run Agent runtime: OpenAI-compatible chat + tool loop (C01–C05)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .memory import (
    MemoryPrefetch,
    format_memories_for_injection,
    start_memory_prefetch,
)
from .prompt import build_system_prompt
from .session import save_session
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
        self._confirm_fn: ConfirmFn | None = None
        self._confirmed: set[str] = set()
        self._aborted = False
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0

        if self.permission_mode == "plan":
            self._enter_plan_mode(announce=False)

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
        self._auto_save()

    def show_cost(self) -> None:
        print_info(
            f"tokens in={self.total_input_tokens} out={self.total_output_tokens} "
            f"(session {self.session_id})"
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
        tokens = data.get("tokens") or {}
        self.total_input_tokens = int(tokens.get("input") or 0)
        self.total_output_tokens = int(tokens.get("output") or 0)
        self._confirmed.clear()
        # If started with --plan, keep plan machinery even after restore.
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

    def _enter_plan_mode(self, *, announce: bool) -> None:
        self.permission_mode = "plan"
        if self._pre_plan_mode is None:
            # Keep previous mode if already set by toggle; else default.
            self._pre_plan_mode = "default"
        self._plan_file_path = self._generate_plan_file_path()
        self._sync_system_prompt()
        if announce:
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")

    def _sync_system_prompt(self) -> None:
        content = self._base_system_prompt
        if self.permission_mode == "plan" and self._plan_file_path:
            content = self._base_system_prompt + self._build_plan_mode_prompt(self._plan_file_path)
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
        """Lightweight no-tools completion for memory recall selection."""
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

    async def chat(self, user_message: str) -> None:
        self._aborted = False
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

            self._consume_memory_prefetch(memory_prefetch)

            try:
                message, usage = await self._call_model()
            except Exception as e:
                print_error(f"model call failed: {e}")
                break

            if usage:
                self.total_input_tokens += int(usage.get("prompt_tokens") or 0)
                self.total_output_tokens += int(usage.get("completion_tokens") or 0)

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
            self.messages.append(
                {"role": "tool", "tool_call_id": tc_id, "content": result}
            )
            return

        if name == "skill":
            result = await self._execute_skill_tool(inp)
            print_tool_result(name, result)
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
        elif perm["action"] == "confirm":
            ok = await self._confirm(perm.get("message") or name)
            if not ok:
                result = "User denied this action."
            else:
                result = await execute_tool(name, inp)
                print_tool_result(name, result)
        else:
            result = await execute_tool(name, inp)
            print_tool_result(name, result)

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
                    "updated_at": time.time(),
                    "tokens": {
                        "input": self.total_input_tokens,
                        "output": self.total_output_tokens,
                    },
                },
            )
        except Exception:
            pass
