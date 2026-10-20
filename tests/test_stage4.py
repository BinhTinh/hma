"""Kiểm thử giai đoạn 4: runner, trạng thái thư, leo thang, cổng thư (không cần mạng)."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from hma import db, kb, runner
from hma.kb import KBDoc
from hma.llm import AssistantTurn, LLMError, ToolCall, Usage
from hma.mailio import MailError
from hma.port import CapturingPort, ImapSmtpPort
from hma.runner import Env, Limits, MailSource, RunLock, RunnerError, run_batch, summarize, write_report
from hma.tools import ToolSettings

SETTINGS = ToolSettings({"customer.test": "client", "partner.test": "partner"},
                        {"client": "alice@corp.test", "partner": "bob@corp.test"}, kb_top_k=3, reply_daily_cap=20)
LIMITS = Limits(max_steps=12, episode_timeout_s=120, episode_token_cap=60000, body_limit_chars=6000, max_run_messages=100)


def raw_mail(frm: str, subject: str, body: str, *, mid: str | None = None) -> bytes:
    mid = mid or f"<{abs(hash((frm, subject))) % 10**8}@x.test>"
    return (f"Return-Path: <{frm}>\r\nFrom: {frm}\r\nTo: support@corp.test\r\nSubject: {subject}\r\nMessage-ID: {mid}\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}").encode("utf-8")


class FakeSource:
    def __init__(self, mails: dict[int, bytes], uidvalidity: int = 111):
        self.mails, self.uidvalidity, self.fetched = mails, uidvalidity, []

    def list_uids(self):
        return list(self.mails)

    def fetch_raw(self, uid):
        self.fetched.append(uid)
        item = self.mails[uid]
        if isinstance(item, Exception):
            raise item
        return item


def turn(calls=(), content=None, total=100):
    tcs = tuple(ToolCall(cid, n, json.dumps(a)) for cid, n, a in calls)
    return AssistantTurn(content, "r", tcs, "tool_calls" if tcs else "stop", Usage(total - 10, 10, total, 2, 5, total - 15), 1)


class FakeLLM:
    def __init__(self, script=()):
        self.script, self.calls = list(script), 0

    def chat(self, messages, tools=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def type2_flow():
    return [turn([("a", "read_message", {}), ("b", "set_task_type", {"task_type": 2})]),
            turn([("c", "reply", {"body": "Chúng tôi làm việc 08:00 đến 17:00."})]), turn(content="xong")]


def type3_flow(priority="normal"):
    return [turn([("a", "set_task_type", {"task_type": 3}), ("b", "create_ticket", {"priority": priority})]),
            turn([("c", "route", {"target": "handoff"})]), turn([("d", "reply", {"body": "Đã ghi nhận."})]), turn(content="xong")]


def type6_flow():
    return [turn([("a", "set_task_type", {"task_type": 6}), ("b", "route", {"target": "archive"})]), turn(content="xong")]


class FailingHandoffPort:
    def send_reply(self, **kw): pass
    def send_handoff(self, **kw): raise ConnectionError("SMTP down")
    def archive(self, uid): pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "a.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        kb.replace_kb(self.conn, [KBDoc("KB-018", "Mức ưu tiên", "Normal: một người.")], ("KB-018",))
        self.port = CapturingPort(None)

    def env(self, llm, *, limits=LIMITS, uidvalidity=111, port=None) -> Env:
        return Env(self.conn, port or self.port, llm, SETTINGS, "SYS", limits, "INBOX", uidvalidity)

    def msg(self, uid):
        return self.conn.execute("SELECT * FROM messages WHERE uid = ?", (uid,)).fetchone()

    def actions(self, uid):
        return [dict(r) for r in self.conn.execute(
            "SELECT a.action_type, a.target, a.status, a.detail FROM actions a JOIN messages m USING(message_key) WHERE m.uid = ? ORDER BY a.id", (uid,))]

    def go(self, mails, llm, **kw):
        say_kw = {k: kw.pop(k) for k in ("limit", "only") if k in kw}
        return run_batch(self.env(llm, **kw), FakeSource(mails), say=lambda s: None, **say_kw)


class BatchTests(Base):
    MAILS = {
        1: raw_mail("customer1@customer.test", "Giờ làm việc?", "Bộ phận hỗ trợ làm việc giờ nào?"),
        2: raw_mail("bob@corp.test", "FYI: họp nhóm", "Họp dời sang thứ Sáu."),
        3: raw_mail("partner1@partner.test", "Bản tin tháng 10", "Đây là bản tin định kỳ."),
        4: raw_mail("customer1@customer.test", "Không đăng nhập được", "Lỗi ERR-AUTH-401, chỉ tôi bị ảnh hưởng."),
    }

    def test_mixed_batch_then_idempotent_rerun(self):
        llm = FakeLLM(type2_flow() + type6_flow() + type3_flow())
        entries = self.go(self.MAILS, llm)
        self.assertEqual([(e["uid"], e["status"]) for e in entries],
                         [(1, "DONE"), (2, "SKIPPED"), (3, "DONE"), (4, "DONE")])
        self.assertEqual(llm.calls, 9)
        self.assertEqual((self.msg(2)["status"], self.msg(2)["reason"]), ("SKIPPED", "sender_not_allowed"))
        self.assertEqual(self.actions(2), [])
        self.assertEqual([a["action_type"] for a in self.actions(4)], ["create_ticket", "handoff", "reply"])
        e1, e3, e4 = entries[0], entries[2], entries[3]
        self.assertEqual(e1["replies"][0]["to"], "customer1@customer.test")
        self.assertTrue(e3["archived"])
        self.assertEqual(e4["tickets"], [{"id": "TKT-0004", "priority": "normal", "status": "open"}])
        self.assertEqual(e4["handoffs"][0]["to"], "alice@corp.test")
        self.assertIn("TKT-0004", e4["replies"][0]["body"])
        self.assertEqual(e4["task_type"], 3)
        self.assertEqual(e1["tokens"]["total"], 300)
        again = self.go(self.MAILS, FakeLLM([]))
        self.assertEqual(again, [])

    def test_summary_and_report(self):
        entries = self.go(self.MAILS, FakeLLM(type2_flow() + type6_flow() + type3_flow()))
        s = summarize(entries)
        self.assertEqual((s["messages"], s["by_status"]), (4, {"DONE": 3, "SKIPPED": 1}))
        self.assertEqual(s["tokens"]["total"], 900)
        out = write_report(Path(self.tmp.name) / "logs" / "r.json", "dry-run", entries, "t0")
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((doc["mode"], len(doc["messages"])), ("dry-run", 4))
        self.assertIn("Giờ làm việc?", out.read_text(encoding="utf-8"))

    def test_limit_and_only(self):
        entries = self.go(self.MAILS, FakeLLM(type2_flow() + type6_flow()), limit=2)
        self.assertEqual([e["uid"] for e in entries], [1, 2])
        entries = self.go(self.MAILS, FakeLLM(type6_flow()), only={3})
        self.assertEqual([e["uid"] for e in entries], [3])
        only_cap = Limits(12, 120, 60000, 6000, max_run_messages=1)
        entries = self.go({5: self.MAILS[1], 6: self.MAILS[1]}, FakeLLM(type2_flow()), limits=only_cap, limit=50)
        self.assertEqual(len(entries), 1)


class EscalationTests(Base):
    def test_truncated_mail_is_escalated_once(self):
        small = Limits(12, 120, 60000, body_limit_chars=30, max_run_messages=100)
        mails = {7: raw_mail("customer1@customer.test", "Lỗi dài", "x" * 200)}
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 3})]), turn([("b", "create_ticket", {"priority": "low"})])])
        [e] = self.go(mails, llm, limits=small)
        self.assertEqual((e["status"], e["reason"], e["escalation"]), ("NEEDS_HUMAN", "needs_human_truncated", "SENT"))
        self.assertTrue(e["handoffs"][0]["subject"].startswith("[CẦN NGƯỜI XỬ LÝ]"))
        self.assertEqual(e["handoffs"][0]["to"], "alice@corp.test")
        self.assertEqual(self.actions(7), [{"action_type": "handoff", "target": "alice@corp.test", "status": "SENT",
                                            "detail": "escalation:needs_human_truncated"}])
        self.assertEqual(self.msg(7)["handoff_status"], "SENT")
        self.assertEqual(self.go(mails, FakeLLM([]), limits=small), [])

    def test_escalation_not_duplicated_when_handoff_already_sent(self):
        mails = {8: raw_mail("partner1@partner.test", "Lỗi API", "Lỗi 500.")}
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 3}), ("b", "create_ticket", {"priority": "high"})]),
                       turn([("c", "route", {"target": "handoff"})]),
                       turn(content="Dừng ở đây, không trả lời."), turn(content="Vẫn không.")])
        [e] = self.go(mails, llm)
        self.assertEqual((e["status"], e["escalation"]), ("NEEDS_HUMAN", "EXISTING"))
        self.assertEqual(len(e["handoffs"]), 1)

    def test_escalation_send_failure_keeps_running(self):
        small = Limits(12, 120, 60000, body_limit_chars=30, max_run_messages=100)
        mails = {1: raw_mail("customer1@customer.test", "Dài", "x" * 200),
                 2: raw_mail("customer1@customer.test", "Giờ làm việc?", "ngắn")}
        port = CapturingPort(FailingHandoffPort())
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 3})]), turn([("b", "create_ticket", {"priority": "low"})]),
                       turn([("c", "set_task_type", {"task_type": 1})]), turn(content="ok")])
        entries = self.go(mails, llm, limits=small, port=port)
        self.assertEqual([(e["uid"], e["status"]) for e in entries], [(1, "NEEDS_HUMAN"), (2, "DONE")])
        self.assertEqual(entries[0]["escalation"], "FAILED")
        self.assertEqual(self.msg(1)["handoff_status"], "FAILED")
        self.assertEqual(self.actions(1)[0]["status"], "FAILED")

    def test_stale_claimed_row_is_escalated_without_llm(self):
        mails = {5: raw_mail("customer1@customer.test", "Giờ làm việc?", "ngắn")}
        key = runner.store.register_message(self.conn, mailbox="INBOX", uidvalidity=111, uid=5, sender="customer1@customer.test",
                                            sender_class="client", subject="s", truncated=False)
        self.conn.execute("UPDATE messages SET status='CLAIMED', attempts=1 WHERE message_key=?", (key,))
        llm = FakeLLM([])
        [e] = self.go(mails, llm)
        self.assertEqual((e["status"], e["reason"], e["escalation"]), ("NEEDS_HUMAN", "interrupted", "SENT"))
        self.assertEqual(llm.calls, 0)


class RetryTests(Base):
    MAILS = {9: raw_mail("customer1@customer.test", "Giờ làm việc?", "Giờ nào?")}

    def test_retryable_failure_then_success(self):
        [e] = self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]))
        self.assertEqual((e["status"], e["attempts"]), ("FAILED", 1))
        self.assertEqual(e.get("escalation"), None)
        [e] = self.go(self.MAILS, FakeLLM(type2_flow()))
        self.assertEqual((e["status"], e["attempts"]), ("DONE", 2))

    def test_attempts_exhausted_becomes_needs_human(self):
        for i in range(3):
            [e] = self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]))
            self.assertEqual((e["status"], e["attempts"]), ("FAILED", i + 1))
        llm = FakeLLM([])
        [e] = self.go(self.MAILS, llm)
        self.assertEqual((e["status"], e["reason"], e["escalation"], llm.calls), ("NEEDS_HUMAN", "retry_exhausted", "SENT", 0))

    def test_failure_after_side_effects_is_not_retried(self):
        mails = {10: raw_mail("customer1@customer.test", "Lỗi đăng nhập", "Không đăng nhập được.")}
        llm = FakeLLM([turn([("a", "set_task_type", {"task_type": 3}), ("b", "create_ticket", {"priority": "normal"})]),
                       LLMError("mạng", retryable=True)])
        [e] = self.go(mails, llm)
        self.assertEqual(e["status"], "NEEDS_HUMAN")
        self.assertTrue(e["reason"].startswith("partial_effects"))
        self.assertEqual(e["tickets"][0]["id"], "TKT-0004")
        self.assertEqual(e["escalation"], "SENT")

    def test_unexpected_exception_marks_failed_and_continues(self):
        mails = {1: raw_mail("customer1@customer.test", "A", "a"), 2: raw_mail("customer1@customer.test", "B", "b")}
        llm = FakeLLM([RuntimeError("boom")] + type2_flow())
        import logging
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        entries = self.go(mails, llm)
        self.assertEqual([(e["uid"], e["status"]) for e in entries], [(1, "FAILED"), (2, "DONE")])
        self.assertIn("boom", entries[0]["reason"])


class GuardTests(Base):
    def test_uidvalidity_change_aborts_before_any_work(self):
        self.go({1: raw_mail("customer1@customer.test", "Giờ?", "giờ nào")}, FakeLLM(type2_flow()))
        llm = FakeLLM([])
        src = FakeSource({1: b"x"}, uidvalidity=222)
        with self.assertRaisesRegex(RunnerError, "UIDVALIDITY"):
            run_batch(self.env(llm, uidvalidity=222), src, say=lambda s: None)
        self.assertEqual((llm.calls, src.fetched), (0, []))

    def test_infrastructure_error_aborts_run_but_keeps_progress(self):
        mails = {1: raw_mail("customer1@customer.test", "Giờ?", "giờ nào"), 2: MailError("IMAP rớt")}
        with self.assertRaises(MailError):
            self.go(mails, FakeLLM(type2_flow()))
        self.assertEqual(self.msg(1)["status"], "DONE")
        self.assertIsNone(self.msg(2))

    def test_run_lock_is_exclusive(self):
        path = Path(self.tmp.name) / "run.lock"
        with RunLock(path):
            with self.assertRaisesRegex(RunnerError, "khóa"):
                with RunLock(path):
                    pass
        with RunLock(path):
            pass


class FakeSmtp:
    def __init__(self): self.sent = []
    def send(self, msg, to_addrs, envelope_from=None): self.sent.append((msg, to_addrs))


class FakeImap:
    def __init__(self): self.moves = []
    def move(self, uid, dest, caps): self.moves.append((uid, dest, caps))


class PortTests(unittest.TestCase):
    def setUp(self):
        self.smtp, self.imap = FakeSmtp(), FakeImap()
        self.port = ImapSmtpPort(self.smtp, self.imap, "support@corp.test", frozenset({"MOVE"}))

    def test_reply_headers(self):
        self.port.send_reply(to="customer1@customer.test", subject="Re: Chào", body="Nội dung", in_reply_to="<a@x>",
                             references="<z@x> <a@x>")
        msg, rcpts = self.smtp.sent[0]
        self.assertEqual(rcpts, ["customer1@customer.test"])
        self.assertEqual((msg["From"], msg["To"], msg["Auto-Submitted"]), ("support@corp.test", "customer1@customer.test", "auto-replied"))
        self.assertEqual((msg["In-Reply-To"], msg["References"]), ("<a@x>", "<z@x> <a@x>"))
        self.assertIn("Nội dung", msg.get_content())

    def test_reply_without_threading_headers(self):
        self.port.send_reply(to="a@customer.test", subject="Re: x", body="b", in_reply_to=None, references=None)
        msg, _ = self.smtp.sent[0]
        self.assertIsNone(msg["In-Reply-To"])
        self.assertIsNone(msg["References"])

    def test_handoff_sets_reply_to(self):
        self.port.send_handoff(to="alice@corp.test", subject="[TKT-0004] x", body="b", reply_to="customer1@customer.test")
        msg, rcpts = self.smtp.sent[0]
        self.assertEqual((rcpts, msg["Reply-To"], msg["To"]), (["alice@corp.test"], "customer1@customer.test", "alice@corp.test"))
        self.assertIsNone(msg["Auto-Submitted"])

    def test_header_injection_is_refused(self):
        with self.assertRaises(ValueError):
            self.port.send_reply(to="a@customer.test", subject="Re: x", body="b", in_reply_to="<a@x>\r\nBcc: e@evil.test", references=None)
        self.assertEqual(self.smtp.sent, [])

    def test_archive_uses_move_with_caps(self):
        self.port.archive(16)
        self.assertEqual(self.imap.moves, [(16, "Archive", frozenset({"MOVE"}))])

    def test_capturing_port_records_only_successes(self):
        class Boom:
            def send_reply(self, **kw): raise ConnectionError("x")
            def send_handoff(self, **kw): pass
            def archive(self, uid): pass
        cp = CapturingPort(Boom())
        with self.assertRaises(ConnectionError):
            cp.send_reply(to="a", subject="s", body="b", in_reply_to=None, references=None)
        cp.send_handoff(to="a", subject="s", body="b", reply_to="c")
        cp.archive(3)
        d = cp.drain()
        self.assertEqual((d["replies"], len(d["handoffs"]), d["archived"]), ([], 1, [3]))
        self.assertEqual(cp.drain(), {"replies": [], "handoffs": [], "archived": []})


if __name__ == "__main__":
    unittest.main()
