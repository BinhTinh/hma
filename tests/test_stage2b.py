"""Kiểm thử giai đoạn 2b: tool + CSDL thật (SQLite tạm) + cổng thư giả."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hma import db, kb, policy, store
from hma.kb import KBDoc
from hma.tools import ARG_SPECS, TOOL_SCHEMAS, MessageContext, ToolSettings, Toolbox
from hma.triage import ParsedMail

SETTINGS = ToolSettings({"customer.test": "client", "partner.test": "partner"},
                        {"client": "alice@corp.test", "partner": "bob@corp.test"}, kb_top_k=3, reply_daily_cap=20)


def mk(**kw) -> ParsedMail:
    base = dict(sender="customer1@customer.test", return_path="customer1@customer.test", return_path_present=True,
                subject="Không đăng nhập được từ sáng nay", message_id="<m1@customer.test>", references=None,
                auto_submitted=None, precedence=None, has_list_header=False,
                body_text="Từ sáng nay tôi không đăng nhập được, báo lỗi ERR-AUTH-401.", truncated=False)
    base.update(kw)
    return ParsedMail(**base)


def partner_mail(**kw) -> ParsedMail:
    base = dict(sender="partner1@partner.test", return_path="partner1@partner.test",
                subject="API trả về lỗi 500 liên tục", message_id="<m2@partner.test>")
    base.update(kw)
    return mk(**base)


class FakePort:
    def __init__(self):
        self.replies, self.handoffs, self.archived, self.fail = [], [], [], set()

    def send_reply(self, **kw):
        if "send_reply" in self.fail:
            raise ConnectionError("SMTP down")
        self.replies.append(kw)

    def send_handoff(self, **kw):
        if "send_handoff" in self.fail:
            raise ConnectionError("SMTP down")
        self.handoffs.append(kw)

    def archive(self, uid):
        if "archive" in self.fail:
            raise ConnectionError("IMAP down")
        self.archived.append(uid)


class World:
    def __init__(self, settings=SETTINGS):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "a.db"
        db.init_db(path)
        self.conn = db.connect(path)
        kb.replace_kb(self.conn, [
            KBDoc("KB-001", "Giờ làm việc của bộ phận hỗ trợ", "Làm việc từ thứ Hai đến thứ Sáu, 08:00 đến 17:00."),
            KBDoc("KB-003", "Chính sách hoàn tiền", "Tiền được hoàn trong 7 ngày làm việc."),
        ], ())
        self.port = FakePort()
        self.settings = settings

    def close(self):
        self.conn.close()
        self.tmp.cleanup()

    def toolbox(self, mail: ParsedMail, uid: int = 7, register: bool = True) -> Toolbox:
        cls = "client" if mail.sender.endswith("customer.test") else "partner"
        key = (store.register_message(self.conn, mailbox="INBOX", uidvalidity=111, uid=uid, sender=mail.sender,
                                      sender_class=cls, subject=mail.subject, truncated=mail.truncated)
               if register else "INBOX:111:999")
        return Toolbox(MessageContext(self.conn, key, uid, mail, self.settings, self.port))

    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)


class DispatchTests(Base):
    def test_validation(self):
        tb = self.w.toolbox(mk())
        cases = [
            (("nope", {}), "unknown_tool"),
            (("set_task_type", {"task_type": "3"}), "invalid_arguments"),
            (("set_task_type", {"task_type": True}), "invalid_arguments"),
            (("set_task_type", {}), "invalid_arguments"),
            (("set_task_type", {"task_type": 3, "x": 1}), "invalid_arguments"),
            (("search_kb", {"query": "  "}), "invalid_arguments"),
            (("search_kb", {"query": "a" * 501}), "invalid_arguments"),
            (("get_ticket", {"ticket_id": "ticket 1"}), "invalid_arguments"),
            (("reply", {"body": "  "}), "invalid_arguments"),
            (("reply", {"body": "x\x00y"}), "invalid_arguments"),
            (("read_message", [1]), "invalid_arguments"),
        ]
        for (name, args), code in cases:
            with self.subTest(name=name, args=args):
                r = tb.call(name, args)
                self.assertFalse(r["ok"])
                self.assertEqual(r["error"], code)

    def test_schemas_match_arg_specs(self):
        names = [s["function"]["name"] for s in TOOL_SCHEMAS]
        self.assertEqual(sorted(names), sorted(ARG_SPECS))
        for s in TOOL_SCHEMAS:
            f = s["function"]
            req, opt = ARG_SPECS[f["name"]]
            self.assertEqual(set(f["parameters"]["properties"]), set(req) | set(opt), f["name"])
            self.assertEqual(sorted(f["parameters"]["required"]), sorted(req), f["name"])

    def test_requires_known_sender(self):
        with self.assertRaises(ValueError):
            Toolbox(MessageContext(self.w.conn, "INBOX:111:1", 1, mk(sender="alice@corp.test"), SETTINGS, self.w.port))


class ReadOnlyToolTests(Base):
    def test_read_message_and_search(self):
        tb = self.w.toolbox(mk(subject="Về TKT-0001: ok"))
        r = tb.call("read_message")
        self.assertTrue(r["ok"])
        self.assertEqual((r["from"], r["sender_class"], r["ticket_ids"]), ("customer1@customer.test", "client", ["TKT-0001"]))
        self.assertIn("dữ liệu", r["note"])
        hits = tb.call("search_kb", {"query": "giờ làm việc"})["results"]
        self.assertEqual(hits[0]["doc_id"], "KB-001")

    def test_get_ticket_rules(self):
        tb = self.w.toolbox(mk(subject="Về TKT-0001: ok"))
        r = tb.call("get_ticket", {"ticket_id": "TKT-0001"})
        self.assertTrue(r["ok"])
        self.assertNotIn("requester", r["ticket"])
        self.assertEqual(tb.call("get_ticket", {"ticket_id": "TKT-0002"})["error"], "ticket_not_found")
        tb2 = self.w.toolbox(partner_mail(subject="Về TKT-0001", body_text="xin ticket TKT-0001"), uid=8)
        self.assertEqual(tb2.call("get_ticket", {"ticket_id": "TKT-0001"})["error"], "ticket_not_found")


class FlowTests(Base):
    def test_type2_reply_from_kb(self):
        mail = mk(subject="Giờ làm việc của bộ phận hỗ trợ?", body_text="Bộ phận hỗ trợ làm việc vào giờ nào?")
        tb = self.w.toolbox(mail)
        r = tb.call("set_task_type", {"task_type": 2})
        self.assertEqual((r["allowed_actions"], r["required_actions"]), (["reply"], ["reply"]))
        self.assertEqual(tb.obligations(), ["reply"])
        self.assertTrue(tb.call("reply", {"body": "Chúng tôi làm việc 08:00 đến 17:00."})["ok"])
        sent = self.w.port.replies[0]
        self.assertEqual(sent["to"], "customer1@customer.test")
        self.assertEqual(sent["subject"], "Re: Giờ làm việc của bộ phận hỗ trợ?")
        self.assertEqual(sent["in_reply_to"], "<m1@customer.test>")
        self.assertIn("08:00 đến 17:00", sent["body"])
        self.assertNotIn("Mã yêu cầu", sent["body"])
        self.assertTrue(sent["body"].endswith("--\nBộ phận hỗ trợ"))
        self.assertEqual(self.w.rows("SELECT action_type, status FROM actions"), [{"action_type": "reply", "status": "SENT"}])
        self.assertEqual(tb.obligations(), [])
        self.assertEqual(tb.call("reply", {"body": "lần hai"})["error"], "already_done")
        self.assertEqual(len(self.w.port.replies), 1)

    def test_type3_customer_goes_to_alice(self):
        tb = self.w.toolbox(mk())
        tb.call("set_task_type", {"task_type": 3})
        r = tb.call("create_ticket", {"priority": "normal"})
        self.assertEqual(r["ticket_id"], "TKT-0004")
        t = self.w.rows("SELECT * FROM tickets WHERE id='TKT-0004'")[0]
        self.assertEqual((t["subject"], t["requester"], t["status"], t["priority"]),
                         ("Không đăng nhập được từ sáng nay", "customer1@customer.test", "open", "normal"))
        self.assertEqual(tb.obligations(), ["handoff", "reply"])
        self.assertEqual(tb.call("reply", {"body": "Đã ghi nhận."})["error"], "handoff_first")
        self.assertEqual(self.w.port.replies, [])
        self.assertTrue(tb.call("route", {"target": "handoff"})["ok"])
        h = self.w.port.handoffs[0]
        self.assertEqual((h["to"], h["reply_to"], h["subject"]),
                         ("alice@corp.test", "customer1@customer.test", "[TKT-0004] Không đăng nhập được từ sáng nay"))
        self.assertIn("Ticket: TKT-0004", h["body"])
        self.assertIn("ERR-AUTH-401", h["body"])
        self.assertTrue(tb.call("reply", {"body": "Chúng tôi đã tạo yêu cầu."})["ok"])
        body = self.w.port.replies[0]["body"]
        self.assertIn("Mã yêu cầu: TKT-0004", body)
        self.assertIn("đã được chuyển cho bộ phận phụ trách", body)
        self.assertEqual(tb.obligations(), [])
        self.assertEqual(self.w.rows("SELECT handoff_status, task_type FROM messages WHERE uid=7"),
                         [{"handoff_status": "SENT", "task_type": 3}])

    def test_partner_goes_to_bob_and_ids_increment(self):
        a = self.w.toolbox(mk(), uid=7)
        a.call("set_task_type", {"task_type": 3})
        a.call("create_ticket", {"priority": "normal"})
        b = self.w.toolbox(partner_mail(), uid=8)
        b.call("set_task_type", {"task_type": 3})
        self.assertEqual(b.call("create_ticket", {"priority": "high"})["ticket_id"], "TKT-0005")
        b.call("route", {"target": "handoff"})
        self.assertEqual(self.w.port.handoffs[0]["to"], "bob@corp.test")

    def test_type5_forward_only(self):
        tb = self.w.toolbox(partner_mail(subject="Hỏi về tích hợp webhook", body_text="Cần hướng dẫn chữ ký webhook."))
        tb.call("set_task_type", {"task_type": 5})
        self.assertEqual(tb.call("create_ticket", {"priority": "normal"})["error"], "action_not_allowed")
        self.assertTrue(tb.call("route", {"target": "handoff"})["ok"])
        self.assertEqual(self.w.port.handoffs[0]["subject"], "[Chuyển tiếp] Hỏi về tích hợp webhook")
        self.assertEqual(tb.obligations(), [])

    def test_type4_reopen_requires_handoff(self):
        tb = self.w.toolbox(mk(subject="Về TKT-0003: lỗi vẫn còn, cần xử lý gấp", body_text="Về TKT-0003, lỗi lặp lại."))
        tb.call("set_task_type", {"task_type": 4})
        r = tb.call("update_ticket", {"ticket_id": "TKT-0003", "status": "open", "priority": "high"})
        self.assertTrue(r["ok"] and r["needs_handoff"])
        t = self.w.rows("SELECT status, priority FROM tickets WHERE id='TKT-0003'")[0]
        self.assertEqual(t, {"status": "open", "priority": "high"})
        self.assertEqual(tb.obligations(), ["handoff"])
        self.assertEqual(tb.call("reply", {"body": "x"})["error"], "handoff_first")
        tb.call("route", {"target": "handoff"})
        self.assertEqual(self.w.port.handoffs[0]["subject"], "[TKT-0003] Về TKT-0003: lỗi vẫn còn, cần xử lý gấp")
        self.assertEqual(tb.obligations(), [])

    def test_type4_resolved_needs_no_handoff(self):
        tb = self.w.toolbox(mk(subject="Về TKT-0001: đã đăng nhập được", body_text="Về TKT-0001, đã được, đóng giúp."))
        tb.call("set_task_type", {"task_type": 4})
        r = tb.call("update_ticket", {"ticket_id": "TKT-0001", "status": "resolved"})
        self.assertTrue(r["ok"] and not r["needs_handoff"])
        self.assertEqual(tb.obligations(), [])
        self.assertEqual(tb.call("update_ticket", {"ticket_id": "TKT-0001", "status": "closed"})["error"], "already_done")

    def test_type4_denials_leave_db_untouched(self):
        tb = self.w.toolbox(partner_mail(subject="Về TKT-0001", body_text="TKT-0001"))
        tb.call("set_task_type", {"task_type": 4})
        self.assertEqual(tb.call("update_ticket", {"ticket_id": "TKT-0001", "status": "resolved"})["error"], "ticket_not_found")
        tb2 = self.w.toolbox(mk(subject="hỏi chung", body_text="không nhắc mã"), uid=9)
        tb2.call("set_task_type", {"task_type": 4})
        self.assertEqual(tb2.call("update_ticket", {"ticket_id": "TKT-0001", "status": "resolved"})["error"], "ticket_not_found")
        self.assertEqual(self.w.rows("SELECT status FROM tickets WHERE id='TKT-0001'"), [{"status": "open"}])
        self.assertEqual(self.w.rows("SELECT COUNT(*) AS n FROM actions"), [{"n": 0}])

    def test_type1_cannot_write(self):
        tb = self.w.toolbox(mk(subject="Cảm ơn", body_text="Cảm ơn nhiều."))
        tb.call("set_task_type", {"task_type": 1})
        for name, args in (("reply", {"body": "x"}), ("create_ticket", {"priority": "low"}), ("route", {"target": "handoff"}),
                           ("route", {"target": "archive"})):
            self.assertEqual(tb.call(name, args)["error"], "action_not_allowed", name)
        self.assertEqual(tb.obligations(), [])
        self.assertEqual(self.w.rows("SELECT COUNT(*) AS n FROM actions"), [{"n": 0}])
        self.assertEqual((self.w.port.replies, self.w.port.handoffs, self.w.port.archived), ([], [], []))

    def test_type6_archive_only(self):
        tb = self.w.toolbox(mk(subject="Mời tham dự webinar", body_text="Đây là thư quảng cáo."), uid=18)
        tb.call("set_task_type", {"task_type": 6})
        self.assertEqual(tb.call("reply", {"body": "x"})["error"], "action_not_allowed")
        self.assertTrue(tb.call("route", {"target": "archive"})["ok"])
        self.assertEqual(self.w.port.archived, [18])
        self.assertEqual(self.w.rows("SELECT action_type, target, status FROM actions"),
                         [{"action_type": "archive", "target": "Archive", "status": "DONE"}])
        self.assertEqual(tb.obligations(), [])

    def test_task_type_locked_after_write(self):
        tb = self.w.toolbox(mk())
        tb.call("set_task_type", {"task_type": 3})
        tb.call("create_ticket", {"priority": "low"})
        self.assertEqual(tb.call("set_task_type", {"task_type": 2})["error"], "task_type_locked")


class FailureTests(Base):
    def test_reply_send_failure_is_at_most_once(self):
        tb = self.w.toolbox(mk())
        tb.call("set_task_type", {"task_type": 2})
        self.w.port.fail.add("send_reply")
        r = tb.call("reply", {"body": "x"})
        self.assertEqual(r["error"], "send_failed")
        self.assertEqual(tb.escalation, "reply_send_failed")
        self.w.port.fail.clear()
        self.assertEqual(tb.call("reply", {"body": "x"})["error"], "already_done")
        self.assertEqual(self.w.port.replies, [])
        row = self.w.rows("SELECT status, detail FROM actions")[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("SMTP down", row["detail"])

    def test_handoff_failure_marks_message(self):
        tb = self.w.toolbox(mk())
        tb.call("set_task_type", {"task_type": 5})
        self.w.port.fail.add("send_handoff")
        self.assertEqual(tb.call("route", {"target": "handoff"})["error"], "send_failed")
        self.assertEqual(tb.escalation, "handoff_send_failed")
        self.assertEqual(self.w.rows("SELECT handoff_status FROM messages"), [{"handoff_status": "FAILED"}])
        self.assertNotIn("handoff", tb.state.done)

    def test_archive_failure(self):
        tb = self.w.toolbox(mk(subject="quảng cáo"), uid=18)
        tb.call("set_task_type", {"task_type": 6})
        self.w.port.fail.add("archive")
        self.assertEqual(tb.call("route", {"target": "archive"})["error"], "send_failed")
        self.assertEqual(tb.escalation, "archive_failed")

    def test_escalation_reasons(self):
        tb = self.w.toolbox(mk(truncated=True))
        tb.call("set_task_type", {"task_type": 3})
        self.assertEqual(tb.call("create_ticket", {"priority": "low"})["error"], "needs_human_truncated")
        self.assertEqual(tb.escalation, "needs_human_truncated")
        tb2 = self.w.toolbox(mk(return_path="x@customer.test"), uid=8)
        tb2.call("set_task_type", {"task_type": 2})
        self.assertEqual(tb2.call("reply", {"body": "x"})["error"], "return_path_mismatch")
        self.assertEqual(tb2.escalation, "return_path_mismatch")

    def test_auto_submitted_blocks_reply_without_escalation(self):
        tb = self.w.toolbox(mk(auto_submitted="auto-generated"))
        tb.call("set_task_type", {"task_type": 2})
        self.assertEqual(tb.call("reply", {"body": "x"})["error"], "auto_submitted")
        self.assertIsNone(tb.escalation)
        self.assertEqual(tb.obligations(), [])

    def test_daily_cap_across_messages(self):
        self.w.settings = ToolSettings(SETTINGS.sender_classes, SETTINGS.handoff, 3, 1)
        a = self.w.toolbox(mk(), uid=7)
        a.call("set_task_type", {"task_type": 2})
        self.assertTrue(a.call("reply", {"body": "một"})["ok"])
        b = self.w.toolbox(mk(message_id="<m9@x>"), uid=8)
        b.call("set_task_type", {"task_type": 2})
        self.assertEqual(b.call("reply", {"body": "hai"})["error"], "recipient_daily_cap")
        self.assertEqual(b.escalation, "recipient_daily_cap")

    def test_rerun_same_message_cannot_duplicate_ticket(self):
        a = self.w.toolbox(mk(), uid=7)
        a.call("set_task_type", {"task_type": 3})
        a.call("create_ticket", {"priority": "normal"})
        again = self.w.toolbox(mk(), uid=7)  # mô phỏng chạy lại: trạng thái trong bộ nhớ mất, CSDL còn
        again.call("set_task_type", {"task_type": 3})
        self.assertEqual(again.call("create_ticket", {"priority": "normal"})["error"], "already_done")
        self.assertEqual(self.w.rows("SELECT COUNT(*) AS n FROM tickets"), [{"n": 4}])

    def test_foreign_key_error_is_not_masked_as_already_done(self):
        tb = self.w.toolbox(mk(), register=False)
        tb.call("set_task_type", {"task_type": 3})
        with self.assertRaises(sqlite3.IntegrityError):
            tb.call("create_ticket", {"priority": "normal"})
        self.assertEqual(self.w.rows("SELECT COUNT(*) AS n FROM tickets"), [{"n": 3}])

    def test_subject_is_single_line_and_bounded(self):
        tb = self.w.toolbox(mk(subject="dòng một\r\n\tdòng hai " + "x" * 300))
        tb.call("set_task_type", {"task_type": 3})
        tid = tb.call("create_ticket", {"priority": "low"})["ticket_id"]
        subject = self.w.rows("SELECT subject FROM tickets WHERE id=?", tid)[0]["subject"]
        self.assertNotIn("\n", subject)
        self.assertNotIn("\t", subject)
        self.assertLessEqual(len(subject), 200)
        self.assertTrue(subject.startswith("dòng một dòng hai"))


if __name__ == "__main__":
    unittest.main()
