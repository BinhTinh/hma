"""Vòng lặp tool-calling cho MỘT thư (giai đoạn 3). Không biết gì về IMAP/SMTP hay CSDL; chỉ nói chuyện với
LLM và Toolbox."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import prompts
from .llm import AssistantTurn, LLMError, Usage

log = logging.getLogger(__name__)
MAX_TOOL_CALLS_PER_TURN = 6
MAX_NUDGES = 1


class ChatClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> AssistantTurn: ...


class ToolRunner(Protocol):
    escalation: str | None

    def call(self, name: str, args: dict | None = None) -> dict: ...
    def obligations(self) -> list[str]: ...


@dataclass
class EpisodeResult:
    status: str                       # done | escalated | incomplete | max_steps | timeout | token_cap | output_truncated | llm_error
    steps: int
    usage: Usage
    final_text: str = ""
    reason: str = ""
    retryable: bool = False
    messages: list[dict] = field(default_factory=list, repr=False)

    @property
    def outcome(self) -> str:
        """DONE | NEEDS_HUMAN | FAILED, để runner đặt trạng thái thư."""
        if self.status == "done":
            return "DONE"
        if self.status == "llm_error" and self.retryable:
            return "FAILED"
        return "NEEDS_HUMAN"


def _tool_message(call_id: str, result: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)}


def run_episode(llm: ChatClient, toolbox: ToolRunner, tools: list[dict], system_prompt: str, task: str, *,
                max_steps: int, timeout_s: float, token_cap: int,
                on_call: Callable[[int, AssistantTurn], None] | None = None,
                clock: Callable[[], float] = time.monotonic) -> EpisodeResult:
    messages: list[dict] = [{"role": "system", "content": system_prompt}, {"role": "user", "content": task}]
    usage, steps, nudges = Usage(), 0, 0
    deadline = clock() + timeout_s

    def finish(status: str, **kw) -> EpisodeResult:
        return EpisodeResult(status=status, steps=steps, usage=usage, messages=messages, **kw)

    def stop_by_limit(status: str, reason: str) -> EpisodeResult:
        """Chạm giới hạn (bước, thời gian, token). Nếu mọi việc bắt buộc đã xong và không có leo thang thì coi là
        hoàn tất: giới hạn chỉ để chặn vòng lặp chạy mãi, không phải để bỏ thư đã làm xong chỉ vì thiếu câu tóm tắt."""
        if not toolbox.escalation and not toolbox.obligations():
            return finish("done", reason=f"{reason}; công việc bắt buộc đã xong")
        return finish(status, reason=reason)

    while True:
        if steps >= max_steps:
            return stop_by_limit("max_steps", f"hết {max_steps} bước")
        if clock() >= deadline:
            return stop_by_limit("timeout", f"quá {timeout_s:.0f}s")
        if usage.total_tokens >= token_cap:
            return stop_by_limit("token_cap", f"đã dùng {usage.total_tokens} token (giới hạn {token_cap})")
        steps += 1
        try:
            turn = llm.chat(messages, tools)
        except LLMError as exc:
            return finish("llm_error", reason=str(exc), retryable=exc.retryable)
        usage = usage + turn.usage
        if on_call is not None:
            on_call(steps, turn)
        messages.append(turn.to_message())

        if turn.finish_reason == "length":
            return finish("output_truncated", reason="LLM bị cắt do giới hạn độ dài đầu ra")

        if turn.tool_calls:
            for i, tc in enumerate(turn.tool_calls):
                if i >= MAX_TOOL_CALLS_PER_TURN:
                    result = {"ok": False, "error": "too_many_tool_calls",
                              "message": f"Chỉ xử lý tối đa {MAX_TOOL_CALLS_PER_TURN} tool mỗi lượt."}
                else:
                    result = _run_one(toolbox, tc.name, tc.arguments)
                messages.append(_tool_message(tc.id, result))
            if toolbox.escalation:
                return finish("escalated", reason=toolbox.escalation, final_text=turn.content or "")
            continue

        missing = toolbox.obligations()
        if not missing:
            return finish("done", final_text=turn.content or "")
        if nudges < MAX_NUDGES:
            nudges += 1
            messages.append({"role": "user", "content": prompts.build_nudge(missing)})
            continue
        return finish("incomplete", reason="còn thiếu: " + ", ".join(missing), final_text=turn.content or "")


def _run_one(toolbox: ToolRunner, name: str, raw_args: str) -> dict:
    try:
        args = json.loads(raw_args) if raw_args and raw_args.strip() else {}
    except ValueError:
        return {"ok": False, "error": "invalid_json", "message": "arguments không phải JSON hợp lệ."}
    if not isinstance(args, dict):
        return {"ok": False, "error": "invalid_arguments", "message": "arguments phải là một đối tượng JSON."}
    return toolbox.call(name, args)
