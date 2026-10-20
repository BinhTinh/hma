"""Giới hạn bước/thời gian/token: chạm giới hạn khi đã xong việc bắt buộc thì vẫn là DONE (giai đoạn 3, bản sửa)."""
from __future__ import annotations

import json
import unittest

from hma.llm import AssistantTurn, ToolCall, Usage
from hma.loop import run_episode


def turn(calls=(), content=None, total=100):
    tcs = tuple(ToolCall(cid, name, json.dumps(args)) for cid, name, args in calls)
    return AssistantTurn(content, "r", tcs, "tool_calls" if tcs else "stop", Usage(total - 10, 10, total), 1)


class FakeToolbox:
    """Chỉ mô phỏng giao diện mà vòng lặp dùng: call, obligations, escalation."""

    def __init__(self, missing_after_first_call=()):
        self.escalation = None
        self.missing = ["set_task_type"]
        self._after = list(missing_after_first_call)
        self.calls = 0

    def call(self, name, args=None):
        self.calls += 1
        self.missing = list(self._after)
        return {"ok": True}

    def obligations(self):
        return list(self.missing)


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return self.script.pop(0)


def run(llm, tb, **kw):
    kw.setdefault("max_steps", 12)
    kw.setdefault("timeout_s", 120)
    kw.setdefault("token_cap", 20000)
    return run_episode(llm, tb, [], "SYS", "task", **kw)


class LimitTests(unittest.TestCase):
    def test_token_cap_after_work_is_done(self):
        tb, llm = FakeToolbox([]), FakeLLM([turn([("a", "x", {})], total=150), turn(content="không cần tới")])
        res = run(llm, tb, token_cap=100)
        self.assertEqual((res.status, res.outcome, res.steps, llm.calls), ("done", "DONE", 1, 1))
        self.assertIn("token", res.reason)
        self.assertIn("đã xong", res.reason)

    def test_token_cap_with_work_missing(self):
        tb, llm = FakeToolbox(["reply"]), FakeLLM([turn([("a", "x", {})], total=150), turn(content="x")])
        res = run(llm, tb, token_cap=100)
        self.assertEqual((res.status, res.outcome, llm.calls), ("token_cap", "NEEDS_HUMAN", 1))

    def test_max_steps_after_work_is_done(self):
        tb, llm = FakeToolbox([]), FakeLLM([turn([("a", "x", {})]), turn([("b", "x", {})]), turn(content="x")])
        res = run(llm, tb, max_steps=1)
        self.assertEqual((res.status, res.steps), ("done", 1))

    def test_max_steps_with_work_missing(self):
        tb, llm = FakeToolbox(["handoff"]), FakeLLM([turn([("a", "x", {})]), turn([("b", "x", {})])])
        res = run(llm, tb, max_steps=1)
        self.assertEqual((res.status, res.outcome), ("max_steps", "NEEDS_HUMAN"))

    def test_timeout_after_work_is_done_and_before_work_is_not(self):
        ticks = iter([0, 0, 500, 500])
        res = run(FakeLLM([turn([("a", "x", {})])]), FakeToolbox([]), timeout_s=120, clock=lambda: next(ticks))
        self.assertEqual(res.status, "done")
        ticks = iter([0, 0, 500, 500])
        res = run(FakeLLM([turn([("a", "x", {})])]), FakeToolbox(["reply"]), timeout_s=120, clock=lambda: next(ticks))
        self.assertEqual(res.status, "timeout")

    def test_escalation_is_never_turned_into_done(self):
        tb = FakeToolbox([])
        tb.escalation = "reply_send_failed"
        res = run(FakeLLM([turn([("a", "x", {})])]), tb)
        self.assertEqual((res.status, res.reason), ("escalated", "reply_send_failed"))

    def test_nothing_done_yet_is_not_done(self):
        tb = FakeToolbox([])
        res = run(FakeLLM([turn(content="chưa làm gì")]), tb, token_cap=0)
        self.assertEqual(res.status, "token_cap")


if __name__ == "__main__":
    unittest.main()
