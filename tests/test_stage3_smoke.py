"""Kiểm thử khung chạy thử (smoke) bằng LLM giả: đảm bảo bộ kiểm tra phân biệt được đúng và sai."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from hma.kb import KBDoc
from hma.llm import AssistantTurn, ToolCall, Usage
from hma.smoke import CASES, run_smoke

CFG = SimpleNamespace(
    kb_always_load=("KB-018",), sender_classes={"customer.test": "client", "partner.test": "partner"},
    handoff={"client": "alice@corp.test", "partner": "bob@corp.test"},
    agent=SimpleNamespace(kb_top_k=3, max_replies_per_recipient_per_day=20, max_steps=12, episode_timeout_s=120,
                          episode_token_cap=50000))
DOCS = [KBDoc("KB-001", "Giờ làm việc", "08:00 đến 17:00."), KBDoc("KB-018", "Mức ưu tiên", "High: toàn bộ dịch vụ.")]


def t(calls=(), content=None):
    tcs = tuple(ToolCall(i, n, json.dumps(a)) for i, n, a in calls)
    return AssistantTurn(content, "r", tcs, "tool_calls" if tcs else "stop", Usage(90, 10, 100), 1)


def script(m08_priority="high"):
    return [
        t([("a", "read_message", {}), ("b", "set_task_type", {"task_type": 2})]),
        t([("c", "reply", {"body": "Chúng tôi làm việc 08:00 đến 17:00."})]), t(content="xong"),
        t([("d", "set_task_type", {"task_type": 3}), ("e", "create_ticket", {"priority": "normal"})]),
        t([("f", "route", {"target": "handoff"})]), t([("g", "reply", {"body": "Đã ghi nhận."})]), t(content="xong"),
        t([("h", "set_task_type", {"task_type": 3}), ("i", "create_ticket", {"priority": m08_priority})]),
        t([("j", "route", {"target": "handoff"})]), t([("k", "reply", {"body": "Đã ghi nhận."})]), t(content="xong"),
        t([("l", "set_task_type", {"task_type": 6}), ("m", "route", {"target": "archive"})]), t(content="xong"),
    ]


class Fake:
    def __init__(self, items):
        self.items = list(items)

    def chat(self, messages, tools=None):
        return self.items.pop(0)


class SmokeTests(unittest.TestCase):
    def test_all_pass_with_good_behaviour(self):
        out = []
        rc = run_smoke(CFG, Fake(script()), DOCS, out.append)
        self.assertEqual(rc, 0, "\n".join(out))
        self.assertIn("Tổng: 4/4 ca đạt.", out[-1])

    def test_wrong_priority_is_caught(self):
        out = []
        rc = run_smoke(CFG, Fake(script(m08_priority="urgent")), DOCS, out.append)
        self.assertEqual(rc, 1)
        text = "\n".join(out)
        self.assertIn("priority phải là high", text)
        self.assertIn("Tổng: 3/4 ca đạt.", text)

    def test_only_and_unknown_case(self):
        out = []
        self.assertEqual(run_smoke(CFG, Fake(script()[:3]), DOCS, out.append, only="M04"), 0)
        self.assertEqual(run_smoke(CFG, Fake([]), DOCS, out.append, only="M99"), 2)
        self.assertEqual([c.name for c in CASES], ["M04", "M07", "M08", "M16"])


if __name__ == "__main__":
    unittest.main()
