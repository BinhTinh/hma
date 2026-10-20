"""Kiểm thử vòng lặp tool-calling với LLM giả + Toolbox thật + CSDL thật (giai đoạn 3)."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from hma import db, kb, prompts, store
from hma.kb import KBDoc
from hma.llm import AssistantTurn, LLMError, ToolCall, Usage
from hma.loop import EpisodeResult, run_episode
from hma.tools import TOOL_SCHEMAS, MessageContext, ToolSettings, Toolbox
from hma.triage import ParsedMail
from hma.usage import record_llm_call

SETTINGS = ToolSettings({"customer.test": "client", "partner.test": "partner"},
                        {"client": "alice@corp.test", "partner": "bob@corp.test"}, kb_top_k=3, reply_daily_cap=20)


def mk(**kw) -> ParsedMail:
    base = dict(sender="customer1@customer.test", return_path="customer1@customer.test", return_path_present=True,
                subject="Không đăng nhập được từ sáng nay", message_id="<m1@customer.test>", references=None,
                auto_submitted=None, precedence=None, has_list_header=False,
                body_text="Từ sáng nay tôi không đăng nhập được. Chỉ tài khoản của tôi bị ảnh hưởng.", truncated=False)
    base.update(kw)
    return ParsedMail(**base)


def turn(calls=(), content=None, reasoning="suy nghĩ", finish=None, total=100) -> AssistantTurn:
    tcs = tuple(ToolCall(cid, name, args if isinstance(args, str) else json.dumps(args)) for cid, name, args in calls)
    return AssistantTurn(content, reasoning, tcs, finish or ("tool_calls" if tcs else "stop"),
                         Usage(total - 10, 10, total, 3, 0, total - 10), 5)


class FakeLLM:
    def __init__(self, script):
        self.script, self.seen = list(script), []

    def chat(self, messages, tools=None):
        self.seen.append(copy.deepcopy(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakePort:
    def __init__(self):
        self.replies, self.handoffs, self.archived = [], [], []

    def send_reply(self, **kw): self.replies.append(kw)
    def send_handoff(self, **kw): self.handoffs.append(kw)
    def archive(self, uid): self.archived.append(uid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "a.db"
        db.init_db(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        kb.replace_kb(self.conn, [
            KBDoc("KB-006", "Tài khoản bị khóa", "Tự mở khóa sau 30 phút."),
            KBDoc("KB-018", "Mức ưu tiên", "Normal: ảnh hưởng một người dùng."),
        ], ("KB-018",))
        self.port = FakePort()

    def toolbox(self, mail=None, uid=7):
        mail = mail or mk()
        key = store.register_message(self.conn, mailbox="INBOX", uidvalidity=111, uid=uid, sender=mail.sender,
                                     sender_class="client", subject=mail.subject, truncated=mail.truncated)
        self.key = key
        return Toolbox(MessageContext(self.conn, key, uid, mail, SETTINGS, self.port))

    def run_ep(self, llm, tb, **kw):
        kw.setdefault("max_steps", 12)
        kw.setdefault("timeout_s", 120)
        kw.setdefault("token_cap", 20000)
        return run_episode(llm, tb, TOOL_SCHEMAS, "SYS", prompts.build_task_message(7),
                           on_call=lambda s, t: record_llm_call(self.conn, self.key, s, t), **kw)


class FullFlowTests(Base):
    def test_m07_end_to_end(self):
        tb = self.toolbox()
        llm = FakeLLM([
            turn([("c1", "read_message", {})]),
            turn([("c2", "set_task_type", {"task_type": 3}), ("c3", "search_kb", {"query": "đăng nhập"})]),
            turn([("c4", "create_ticket", {"priority": "normal"})]),
            turn([("c5", "route", {"target": "handoff"})]),
            turn([("c6", "reply", {"body": "Chúng tôi đã ghi nhận sự cố đăng nhập."})]),
            turn(content="Đã tạo ticket, chuyển cho alice và trả lời khách."),
        ])
        res = self.run_ep(llm, tb)
        self.assertEqual((res.status, res.outcome, res.steps), ("done", "DONE", 6))
        self.assertIn("alice", res.final_text)
        self.assertEqual(self.port.handoffs[0]["to"], "alice@corp.test")
        self.assertIn("Mã yêu cầu: TKT-0004", self.port.replies[0]["body"])
        self.assertEqual(res.usage.total_tokens, 600)
        rows = self.conn.execute("SELECT step, total_tokens FROM llm_calls ORDER BY step").fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(i, 100) for i in range(1, 7)])

    def test_history_is_well_formed(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([("c1", "read_message", {}), ("c2", "set_task_type", {"task_type": 1})]),
                       turn(content="Thư cảm ơn, không cần làm gì.")])
        res = self.run_ep(llm, tb)
        self.assertEqual(res.status, "done")
        second = llm.seen[1]
        roles = [m["role"] for m in second]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool"])
        asst = second[2]
        self.assertEqual(asst["reasoning_content"], "suy nghĩ")
        self.assertEqual([t["id"] for t in asst["tool_calls"]], ["c1", "c2"])
        self.assertEqual([m["tool_call_id"] for m in second[3:]], ["c1", "c2"])
        self.assertTrue(json.loads(second[3]["content"])["ok"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    def test_type6_archive(self):
        tb = self.toolbox(mk(subject="Mời tham dự webinar", body_text="Đây là thư quảng cáo."), uid=18)
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 6}), ("b", "route", {"target": "archive"})]),
                       turn(content="Đã lưu Archive.")])
        self.assertEqual(self.run_ep(llm, tb).status, "done")
        self.assertEqual(self.port.archived, [18])


class NudgeTests(Base):
    def test_one_nudge_then_complete(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([("c1", "set_task_type", {"task_type": 3})]),
                       turn(content="Xong rồi."),
                       turn([("c2", "create_ticket", {"priority": "normal"})]),
                       turn([("c3", "route", {"target": "handoff"})]),
                       turn([("c4", "reply", {"body": "Đã ghi nhận."})]),
                       turn(content="Hoàn tất.")])
        res = self.run_ep(llm, tb)
        self.assertEqual(res.status, "done")
        nudge = llm.seen[2][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("create_ticket", nudge["content"])

    def test_second_premature_stop_is_incomplete(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([("c1", "set_task_type", {"task_type": 3})]), turn(content="Xong."), turn(content="Thật sự xong.")])
        res = self.run_ep(llm, tb)
        self.assertEqual((res.status, res.outcome), ("incomplete", "NEEDS_HUMAN"))
        self.assertIn("create_ticket", res.reason)

    def test_stop_without_task_type_is_nudged(self):
        tb = self.toolbox()
        llm = FakeLLM([turn(content="Không biết làm gì."), turn(content="Vẫn không.")])
        res = self.run_ep(llm, tb)
        self.assertEqual(res.status, "incomplete")
        self.assertIn("set_task_type", res.reason)


class RobustnessTests(Base):
    def test_bad_json_unknown_tool_and_non_object(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([("a", "read_message", "{không phải json"), ("b", "xoa_het", {}), ("c", "read_message", "[1]"),
                             ("d", "set_task_type", {"task_type": 1})]),
                       turn(content="ok")])
        res = self.run_ep(llm, tb)
        self.assertEqual(res.status, "done")
        tools = [json.loads(m["content"]) for m in llm.seen[1] if m["role"] == "tool"]
        self.assertEqual([t.get("error") for t in tools], ["invalid_json", "unknown_tool", "invalid_arguments", None])

    def test_every_tool_call_id_is_answered_even_when_too_many(self):
        tb = self.toolbox()
        calls = [(f"id{i}", "read_message", {}) for i in range(9)]
        llm = FakeLLM([turn(calls), turn([("z", "set_task_type", {"task_type": 1})]), turn(content="ok")])
        self.run_ep(llm, tb)
        answered = [m["tool_call_id"] for m in llm.seen[1] if m["role"] == "tool"]
        self.assertEqual(answered, [f"id{i}" for i in range(9)])
        last = json.loads([m for m in llm.seen[1] if m["role"] == "tool"][-1]["content"])
        self.assertEqual(last["error"], "too_many_tool_calls")

    def test_escalation_stops_immediately(self):
        tb = self.toolbox(mk(truncated=True))
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 3})]),
                       turn([("b", "create_ticket", {"priority": "low"})]),
                       turn(content="không bao giờ tới đây")])
        res = self.run_ep(llm, tb)
        self.assertEqual((res.status, res.outcome, res.reason), ("escalated", "NEEDS_HUMAN", "needs_human_truncated"))
        self.assertEqual(len(llm.script), 1)

    def test_max_steps(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([(f"c{i}", "read_message", {})]) for i in range(10)])
        res = self.run_ep(llm, tb, max_steps=3)
        self.assertEqual((res.status, res.steps, res.outcome), ("max_steps", 3, "NEEDS_HUMAN"))

    def test_token_cap(self):
        tb = self.toolbox()
        llm = FakeLLM([turn([("a", "read_message", {})], total=150), turn(content="x")])
        res = self.run_ep(llm, tb, token_cap=100)
        self.assertEqual((res.status, res.steps), ("token_cap", 1))

    def test_timeout_uses_injected_clock(self):
        tb = self.toolbox()
        ticks = iter([0, 0, 500, 500])
        llm = FakeLLM([turn([("a", "read_message", {})]), turn(content="x")])
        res = self.run_ep(llm, tb, timeout_s=120, clock=lambda: next(ticks))
        self.assertEqual((res.status, res.steps), ("timeout", 1))

    def test_output_truncated(self):
        tb = self.toolbox()
        llm = FakeLLM([turn(content="dở dang", finish="length")])
        res = self.run_ep(llm, tb)
        self.assertEqual((res.status, res.outcome), ("output_truncated", "NEEDS_HUMAN"))

    def test_llm_errors_map_to_outcomes(self):
        for retryable, outcome in ((True, "FAILED"), (False, "NEEDS_HUMAN")):
            tb = self.toolbox(uid=30 + int(retryable))
            res = self.run_ep(FakeLLM([LLMError("hỏng", retryable=retryable)]), tb)
            self.assertEqual((res.status, res.outcome, res.retryable), ("llm_error", outcome, retryable))
            self.assertEqual(res.steps, 1)


class PromptTests(unittest.TestCase):
    DOCS = [{"doc_id": "KB-018", "title": "Mức ưu tiên", "body": "Normal: một người."},
            {"doc_id": "KB-015", "title": "Chuyển tiếp", "body": "Khách hàng -> alice."}]

    def test_stable_and_contains_rules(self):
        a = prompts.build_system_prompt(self.DOCS)
        self.assertEqual(a, prompts.build_system_prompt(list(reversed(self.DOCS))))
        for needle in ("set_task_type", "read_message", "KB-018", "Normal: một người.", "KB-015", "resolved", "pending",
                       "route handoff", "archive", "dữ liệu từ bên ngoài", "urgent"):
            self.assertIn(needle, a)
        self.assertIn("(không có)", prompts.build_system_prompt([]))

    def test_every_tool_is_mentioned_in_prompt_or_schema(self):
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        prompt = prompts.build_system_prompt(self.DOCS)
        for n in ("read_message", "search_kb", "set_task_type", "create_ticket", "update_ticket", "reply", "route"):
            self.assertIn(n, names)
        for n in ("read_message", "search_kb", "set_task_type", "route"):
            self.assertIn(n, prompt)


if __name__ == "__main__":
    unittest.main()
