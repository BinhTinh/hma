"""Giai đoạn 7a: nền tảng chạy dịch vụ (khóa êm, không báo cáo rác, dấu còn sống, giãn cách thử lại, ngân sách token)."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from hma import db, health, kb, store
from hma.kb import KBDoc
from hma.llm import AssistantTurn, LLMError, ToolCall, Usage
from hma.port import CapturingPort
from hma.runner import (Env, Limits, RunnerError, acquire_lock, finish_pass, run_batch, tokens_today)
from hma.tools import ToolSettings

SETTINGS = ToolSettings({"customer.test": "client", "partner.test": "partner"},
                        {"client": "alice@corp.test", "partner": "bob@corp.test"}, kb_top_k=3, reply_daily_cap=20)


def raw_mail(subject, frm="customer1@customer.test"):
    return (f"Return-Path: <{frm}>\r\nFrom: {frm}\r\nTo: support@corp.test\r\nSubject: {subject}\r\nMessage-ID: <{abs(hash(subject)) % 10**8}@x>\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNội dung {subject}").encode("utf-8")


class FakeSource:
    uidvalidity = 111

    def __init__(self, mails):
        self.mails = mails

    def list_uids(self):
        return list(self.mails)

    def fetch_raw(self, uid):
        return self.mails[uid]


def turn(calls=(), content=None, total=100):
    tcs = tuple(ToolCall(cid, n, json.dumps(a)) for cid, n, a in calls)
    return AssistantTurn(content, "r", tcs, "tool_calls" if tcs else "stop", Usage(total - 10, 10, total), 1)


def flow(total=100):
    """Một thư loại 1 chỉ tốn một lượt gọi LLM; total là số token của lượt đó."""
    return [turn([("a", "set_task_type", {"task_type": 1})], total=total), turn(content="xong", total=total)]


class FakeLLM:
    def __init__(self, script=()):
        self.script, self.calls = list(script), 0

    def chat(self, messages, tools=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "a.db"
        db.init_db(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        kb.replace_kb(self.conn, [KBDoc("KB-018", "Mức ưu tiên", "Normal: một người.")], ("KB-018",))
        self.offset = 0.0
        self.clock_value = 0.0

    def env(self, llm, *, budget=0, retry_wait=(0.0, 0.0), clock=None):
        limits = Limits(12, 120, 60000, 6000, 100, daily_token_budget=budget)
        return Env(self.conn, CapturingPort(None), llm, SETTINGS, "SYS", limits, "INBOX", 111, retry_wait=retry_wait,
                   now=lambda: datetime.now() + timedelta(seconds=self.offset), clock=clock or (lambda: self.clock_value))

    def go(self, mails, llm, **kw):
        run_kw = {k: kw.pop(k) for k in ("deadline", "heartbeat", "limit") if k in kw}
        env = self.env(llm, **kw)
        entries = run_batch(env, FakeSource(mails), say=lambda s: None, **run_kw)
        return entries, env

    def add_usage(self, tokens, created_at):
        key = store.register_message(self.conn, mailbox="INBOX", uidvalidity=111, uid=900 + abs(hash(created_at)) % 90,
                                     sender="customer1@customer.test", sender_class="client", subject="x", truncated=False)
        self.conn.execute("INSERT INTO llm_calls(message_key, step, total_tokens, created_at) VALUES (?,?,?,?)",
                          (key, 1, tokens, created_at))


class BackoffTests(Base):
    MAILS = {5: raw_mail("Giờ làm việc?")}
    WAIT = (120.0, 600.0)

    def test_failed_mail_waits_then_retries_then_waits_longer(self):
        entries, _ = self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]), retry_wait=self.WAIT)
        self.assertEqual((entries[0]["status"], entries[0]["attempts"]), ("FAILED", 1))
        llm = FakeLLM(flow())
        self.offset = 60
        entries, _ = self.go(self.MAILS, llm, retry_wait=self.WAIT)
        self.assertEqual((entries, llm.calls), ([], 0))
        self.offset = 125
        entries, _ = self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]), retry_wait=self.WAIT)
        self.assertEqual((entries[0]["status"], entries[0]["attempts"]), ("FAILED", 2))
        self.offset = 125 + 300
        entries, _ = self.go(self.MAILS, FakeLLM(flow()), retry_wait=self.WAIT)
        self.assertEqual(entries, [])
        self.offset = 125 + 700
        entries, _ = self.go(self.MAILS, FakeLLM(flow()), retry_wait=self.WAIT)
        self.assertEqual((entries[0]["status"], entries[0]["attempts"]), ("DONE", 3))

    def test_exhausted_attempts_escalate_immediately_despite_wait(self):
        for i in range(3):
            self.offset = i * 1000
            entries, _ = self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]), retry_wait=self.WAIT)
            self.assertEqual(entries[0]["attempts"], i + 1)
        self.offset = 10  # updated_at ghi bằng giờ thật nên offset chính là thời gian đã trôi: chỉ 10 giây, vẫn phải leo thang ngay
        llm = FakeLLM([])
        entries, _ = self.go(self.MAILS, llm, retry_wait=self.WAIT)
        self.assertEqual((entries[0]["status"], entries[0]["reason"], llm.calls), ("NEEDS_HUMAN", "retry_exhausted", 0))

    def test_zero_wait_disables_backoff(self):
        self.go(self.MAILS, FakeLLM([LLMError("mạng", retryable=True)]))
        entries, _ = self.go(self.MAILS, FakeLLM(flow()))
        self.assertEqual(entries[0]["status"], "DONE")


class BudgetTests(Base):
    MAILS = {1: raw_mail("A"), 2: raw_mail("B"), 3: raw_mail("C")}

    def test_stops_when_budget_already_spent_today(self):
        self.add_usage(1000, datetime.now().replace(microsecond=0).isoformat())
        llm = FakeLLM(flow())
        entries, env = self.go(self.MAILS, llm, budget=1000)
        self.assertEqual((entries, llm.calls, env.budget_exhausted), ([], 0, True))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE uid IN (1,2,3)").fetchone()[0], 0)

    def test_yesterdays_usage_does_not_count(self):
        yesterday = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat()
        self.add_usage(10_000_000, yesterday)
        self.assertEqual(tokens_today(self.conn, datetime.now().date().isoformat()), 0)
        entries, env = self.go({1: self.MAILS[1]}, FakeLLM(flow()), budget=1000)
        self.assertEqual((len(entries), env.budget_exhausted), (1, False))

    def test_budget_zero_means_unlimited(self):
        self.add_usage(10_000_000, datetime.now().replace(microsecond=0).isoformat())
        entries, env = self.go({1: self.MAILS[1]}, FakeLLM(flow()), budget=0)
        self.assertEqual((len(entries), env.budget_exhausted), (1, False))

    def test_budget_runs_out_midway_and_resumes_next_day(self):
        entries, env = self.go(self.MAILS, FakeLLM(flow(150) + flow(150) + flow(150)), budget=500)
        self.assertEqual((len(entries), env.budget_exhausted), (2, True))   # 300 + 300 = 600 >= 500 sau thư thứ hai
        self.offset = 24 * 3600 + 5
        entries, env = self.go(self.MAILS, FakeLLM(flow()), budget=500)
        self.assertEqual(([e["uid"] for e in entries], env.budget_exhausted), ([3], False))


class DeadlineAndHeartbeatTests(Base):
    MAILS = {1: raw_mail("A"), 2: raw_mail("B"), 3: raw_mail("C")}

    def test_deadline_stops_taking_new_mail(self):
        times = iter([0, 100, 100, 100])
        llm = FakeLLM(flow() + flow() + flow())
        entries, _ = self.go(self.MAILS, llm, deadline=50, clock=lambda: next(times))
        self.assertEqual([e["uid"] for e in entries], [1])

    def test_heartbeat_called_at_start_and_after_each_mail(self):
        seen = []
        entries, _ = self.go(self.MAILS, FakeLLM(flow() * 3), heartbeat=lambda es: seen.append(len(es)))
        self.assertEqual(seen, [0, 1, 2, 3])


class FinishPassTests(unittest.TestCase):
    def test_no_report_when_nothing_processed_but_heartbeat_written(self):
        with tempfile.TemporaryDirectory() as t:
            hb = Path(t) / "data" / "heartbeat.json"
            rep = finish_pass([], mode="live", started="s", report_dir=Path(t) / "logs", stamp="X", heartbeat_path=hb)
            self.assertIsNone(rep)
            self.assertFalse((Path(t) / "logs").exists())
            doc = health.read_heartbeat(hb)
            self.assertEqual((doc["state"], doc["processed"], doc["budget_exhausted"]), ("idle", 0, False))

    def test_report_written_when_mail_processed(self):
        with tempfile.TemporaryDirectory() as t:
            entry = {"uid": 1, "status": "DONE", "tokens": {"total": 5}}
            rep = finish_pass([entry], mode="live", started="s", report_dir=Path(t) / "logs", stamp="X",
                              heartbeat_path=Path(t) / "hb.json", budget_exhausted=True)
            self.assertTrue(rep.is_file())
            self.assertTrue(health.read_heartbeat(Path(t) / "hb.json")["budget_exhausted"])

    def test_dry_run_has_no_heartbeat(self):
        with tempfile.TemporaryDirectory() as t:
            finish_pass([], mode="dry-run", started="s", report_dir=Path(t), stamp="X", heartbeat_path=None)
            self.assertEqual(list(Path(t).iterdir()), [])


class LockTests(unittest.TestCase):
    def test_if_idle_is_quiet_and_default_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            lock = Path(t) / "run.lock"
            with contextlib.ExitStack() as first:
                self.assertTrue(acquire_lock(first, lock, if_idle=False))
                with contextlib.ExitStack() as second:
                    self.assertFalse(acquire_lock(second, lock, if_idle=True))
                with contextlib.ExitStack() as third:
                    with self.assertRaises(RunnerError):
                        acquire_lock(third, lock, if_idle=False)
            with contextlib.ExitStack() as again:
                self.assertTrue(acquire_lock(again, lock, if_idle=True))


class HealthTests(unittest.TestCase):
    def test_check_states(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "hb.json"
            self.assertEqual(health.check(p, 300)[0], 2)
            health.write_heartbeat(p, state="idle", processed=3)
            code, msg = health.check(p, 300)
            self.assertEqual(code, 0)
            self.assertIn("idle", msg)
            self.assertEqual(health.check(p, 300, now=lambda: time.time() + 1000)[0], 1)
            p.write_text("{hỏng", encoding="utf-8")
            self.assertEqual(health.check(p, 300)[0], 2)
            p.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
            self.assertEqual(health.check(p, 300)[0], 2)
            p.write_text("[1]", encoding="utf-8")
            self.assertEqual(health.check(p, 300)[0], 2)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "sub" / "hb.json"
            health.write_heartbeat(p, state="running")
            health.write_heartbeat(p, state="idle")
            self.assertEqual([f.name for f in p.parent.iterdir()], ["hb.json"])
            self.assertEqual(health.read_heartbeat(p)["state"], "idle")

    def test_budget_flag_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "hb.json"
            health.write_heartbeat(p, state="idle", processed=0, budget_exhausted=True)
            self.assertIn("ngân sách token", health.check(p, 300)[1])


if __name__ == "__main__":
    unittest.main()
