"""Run Agent runtime: OpenAI-compatible chat + tool loop (C01)."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .prompt import build_system_prompt
from .session import save_session
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
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt()},
        ]
        self.openai_tools = to_openai_tools(TOOL_DEFINITIONS)

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._confirm_fn: ConfirmFn | None = None
        self._confirmed: set[str] = set()
        self._aborted = False

    def set_confirm_fn(self, fn: ConfirmFn) -> None:
        self._confirm_fn = fn

    def abort(self) -> None:
        self._aborted = True

    def clear_history(self) -> None:
        self.messages = [{"role": "system", "content": build_system_prompt()}]
        self._confirmed.clear()

    def show_cost(self) -> None:
        print_info(
            f"tokens in={self.total_input_tokens} out={self.total_output_tokens} "
            f"(session {self.session_id})"
        )

    async def chat(self, user_message: str) -> None:
        self._aborted = False
        self.messages.append({"role": "user", "content": user_message})

        turns = 0
        while True:
            if self._aborted:
                break
            if turns >= self.max_turns:
                print_warning(f"Stopped: reached max_turns={self.max_turns}")
                break

            try:
                message, usage = await self._call_model()
            except Exception as e:
                print_error(f"model call failed: {e}")
                break

            if usage:
                self.total_input_tokens += int(usage.get("prompt_tokens") or 0)
                self.total_output_tokens += int(usage.get("completion_tokens") or 0)

            # 规范化 assistant message 再入历史
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
            # 多数 OpenAI-compatible 网关用 extra_body；也尝试顶层字段
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as first_err:
            # 若网关不认 extra_body 里的字段，去掉再试一次（无思考强度）
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
        perm = check_permission(self.permission_mode, name, inp)

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
