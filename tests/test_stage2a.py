"""Kiểm thử giai đoạn 2a (không cần mạng, không cần CSDL)."""
from __future__ import annotations

import unittest
from email.message import EmailMessage

from hma import policy as P
from hma.triage import (ParsedMail, extract_ticket_ids, parse_mail, reply_block, sender_class, subject_key)

MAP = {"customer.test": "client", "partner.test": "partner"}

MAILBOX_SUBJECTS = [
    "Thông báo: bảo trì hệ thống phíađối tác", "FYI: họp nhóm dời sang thứ Sáu",
    "Giờ làm việc của bộ phận hỗ trợ?", "Quên mật khẩu cổng khách hàng", "Thời gian hoàn tiền",
    "Không đăng nhập được từ sáng nay", "API trả về lỗi 500 liên tục", "Yêu cầu xuất hóa đơn tháng 9",
    "Về TKT-0001: đã đăng nhập được", "Về TKT-0002: sẽ gửi log vào thứ Hai",
    "Về TKT-0003: lỗi vẫn còn, cần xử lý gấp", "Gia hạn hợp đồng hợp tác", "Góp ý về chất lượng dịch vụ",
    "Hỏi về tích hợp webhook", "Bản tin đối tác tháng 10", "[Tự động] Báo cáo hệ thống hằng ngày",
    "Mời tham dự webinar tháng 10", "Ghi lại: thỏa thuận gia hạn hỗ trợ", "Ghi lại: lịch bảo trì staging",
    "Ghi lại: quy ước ưu tiên ticket của partner", "Không nhận được email xác thực",
    "API bị giới hạn 429, cần tăng quota", "Tài khoản bị khóa", "Cảm ơn bộ phận hỗ trợ",
]


def raw_mail(body="Nội dung", *, frm="customer1@customer.test", rp="<customer1@customer.test>",
             subject="Chủ đề", extra="", ctype="text/plain; charset=utf-8", body_bytes=None) -> bytes:
    head = f"From: {frm}\r\nTo: support@corp.test\r\nSubject: {subject}\r\nMessage-ID: <x@y.test>\r\n"
    if rp is not None:
        head = f"Return-Path: {rp}\r\n" + head
    head += extra + f"MIME-Version: 1.0\r\nContent-Type: {ctype}\r\n\r\n"
    return head.encode("utf-8") + (body_bytes if body_bytes is not None else body.encode("utf-8"))


def mk(**kw) -> ParsedMail:
    base = dict(sender="customer1@customer.test", return_path="customer1@customer.test", return_path_present=True,
                subject="s", message_id="<m@x>", references=None, auto_submitted=None, precedence=None,
                has_list_header=False, body_text="Nội dung", truncated=False)
    base.update(kw)
    return ParsedMail(**base)


class ParseMailTests(unittest.TestCase):
    def test_plain(self):
        m = parse_mail(raw_mail("Xin chào\r\n\r\n\r\n\r\nTạm biệt"), 1000)
        self.assertEqual(m.sender, "customer1@customer.test")
        self.assertEqual(m.return_path, "customer1@customer.test")
        self.assertEqual(m.body_text, "Xin chào\n\nTạm biệt")
        self.assertFalse(m.truncated)

    def test_plain_preferred_over_html(self):
        e = EmailMessage()
        e["From"], e["To"], e["Subject"] = "a@customer.test", "support@corp.test", "s"
        e.set_content("bản thuần")
        e.add_alternative("<p>bản html</p>", subtype="html")
        self.assertEqual(parse_mail(e.as_bytes(), 1000).body_text, "bản thuần")

    def test_html_only_stripped(self):
        html = "<style>p{}</style><p>Xin&nbsp;chào <b>bạn</b></p><script>x()</script><div>Dòng hai</div>"
        e = EmailMessage()
        e["From"], e["To"], e["Subject"] = "a@customer.test", "support@corp.test", "s"
        e.set_content(html, subtype="html")
        body = parse_mail(e.as_bytes(), 1000).body_text
        self.assertIn("Xin chào bạn", body.replace("\xa0", " "))
        self.assertIn("Dòng hai", body)
        self.assertNotIn("x()", body)
        self.assertNotIn("p{}", body)

    def test_unknown_charset_does_not_crash(self):
        m = parse_mail(raw_mail(ctype="text/plain; charset=no-such-charset", body_bytes=b"abc \xff xyz"), 1000)
        self.assertIn("abc", m.body_text)

    def test_truncation_flag(self):
        m = parse_mail(raw_mail("a" * 100), 40)
        self.assertTrue(m.truncated)
        self.assertEqual(len(m.body_text), 40)
        self.assertFalse(parse_mail(raw_mail("a" * 40), 40).truncated)

    def test_attachment_only_has_empty_body(self):
        e = EmailMessage()
        e["From"], e["To"], e["Subject"] = "a@customer.test", "support@corp.test", "s"
        e.set_content("")
        e.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="a.pdf")
        self.assertEqual(parse_mail(e.as_bytes(), 1000).body_text, "")

    def test_return_path_variants(self):
        self.assertFalse(parse_mail(raw_mail(rp=None), 100).return_path_present)
        null = parse_mail(raw_mail(rp="<>"), 100)
        self.assertTrue(null.return_path_present)
        self.assertEqual(null.return_path, "")

    def test_bad_limit(self):
        with self.assertRaises(ValueError):
            parse_mail(raw_mail(), 0)


class HelperTests(unittest.TestCase):
    def test_sender_class_exact_domain(self):
        self.assertEqual(sender_class("A@Customer.test", MAP), "client")
        self.assertEqual(sender_class("p@partner.test", MAP), "partner")
        for bad in ("x@evilcustomer.test", "x@a.customer.test", "x@customer.test.evil.com", "customer.test",
                    "@customer.test", "x@", "a@b@customer.test", "", "alice@corp.test"):
            self.assertIsNone(sender_class(bad, MAP), bad)

    def test_extract_ticket_ids(self):
        self.assertEqual(extract_ticket_ids("Về TKT-0001: ok", "xem TKT-0002 và TKT-0001"), ["TKT-0001", "TKT-0002"])
        self.assertEqual(extract_ticket_ids("tkt-0001 TKT-00012 xTKT-0003 TKT-3"), [])
        self.assertEqual(extract_ticket_ids(None, ""), [])

    def test_subject_key_tolerates_lost_space_and_is_unique(self):
        a = subject_key("Thông báo: bảo trì hệ thống phía đối tác")
        b = subject_key("Thông báo: bảo trì hệ thống phíađối tác")
        self.assertEqual(a, b)
        keys = [subject_key(s) for s in MAILBOX_SUBJECTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(subject_key("ĐĂNG  nhập"), subject_key("đăng nhập"))


class ReplyBlockTests(unittest.TestCase):
    def test_allowed(self):
        self.assertIsNone(reply_block(mk()))
        self.assertIsNone(reply_block(mk(auto_submitted="no")))

    def test_forbid_cases(self):
        for kw, code in (({"auto_submitted": "auto-replied"}, "auto_submitted"),
                         ({"auto_submitted": "auto-generated; x=y"}, "auto_submitted"),
                         ({"has_list_header": True}, "bulk_mail"),
                         ({"precedence": "Bulk"}, "bulk_mail"),
                         ({"return_path": ""}, "null_return_path")):
            b = reply_block(mk(**kw))
            self.assertEqual((b.kind, b.code), ("forbid", code), kw)

    def test_human_cases(self):
        b = reply_block(mk(return_path=None, return_path_present=False))
        self.assertEqual((b.kind, b.code), ("human", "missing_return_path"))
        b = reply_block(mk(return_path="other@customer.test"))
        self.assertEqual((b.kind, b.code), ("human", "return_path_mismatch"))


def state_for(t, **kw):
    s = P.MessageState(task_type=t)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


TICKET = {"id": "TKT-0001", "requester": "customer1@customer.test", "status": "open", "priority": "normal"}


class MatrixTests(unittest.TestCase):
    def _try(self, action, state, mail):
        if action == "create_ticket":
            return P.check_create_ticket(state, mail, "normal")
        if action == "update_ticket":
            return P.check_update_ticket(state, mail, ["TKT-0001"], "TKT-0001", TICKET, "resolved", None)
        if action == "reply":
            return P.check_reply(state, mail, 0, 20)
        return P.check_route(state, mail, action, "alice@corp.test")

    def test_every_type_every_action(self):
        for t in P.TASK_TYPES:
            for action in ("create_ticket", "update_ticket", "reply", "handoff", "archive"):
                with self.subTest(task_type=t, action=action):
                    d = self._try(action, state_for(t), mk())
                    self.assertEqual(d.ok, action in P.ALLOWED[t], d)

    def test_task_type_required_first(self):
        d = P.check_reply(P.MessageState(), mk(), 0, 20)
        self.assertEqual(d.code, "task_type_required")

    def test_set_task_type(self):
        self.assertTrue(P.check_set_task_type(P.MessageState(), 3).ok)
        for bad in (7, 0, "3", None, True, 3.0 + 0.5):
            self.assertEqual(P.check_set_task_type(P.MessageState(), bad).code, "invalid_task_type", bad)
        s = P.MessageState(task_type=3)
        s.record("create_ticket")
        self.assertEqual(P.check_set_task_type(s, 2).code, "task_type_locked")

    def test_escalation_on_truncated_or_empty(self):
        for kw, code in (({"truncated": True}, "needs_human_truncated"), ({"body_text": "  "}, "needs_human_empty_body")):
            d = P.check_create_ticket(state_for(3), mk(**kw), "normal")
            self.assertEqual((d.ok, d.code, d.escalate), (False, code, True))

    def test_already_done(self):
        s = state_for(3)
        s.record("create_ticket", needs_handoff=True)
        self.assertEqual(P.check_create_ticket(s, mk(), "normal").code, "already_done")

    def test_invalid_priority(self):
        self.assertEqual(P.check_create_ticket(state_for(3), mk(), "URGENT").code, "invalid_priority")
        self.assertEqual(P.check_create_ticket(state_for(3), mk(), None).code, "invalid_priority")


class UpdateTicketTests(unittest.TestCase):
    def _u(self, ticket=TICKET, ids=("TKT-0001",), status="resolved", priority=None, mail=None):
        return P.check_update_ticket(state_for(4), mail or mk(), list(ids), "TKT-0001", ticket, status, priority)

    def test_ok_resolved_and_pending_do_not_need_handoff(self):
        for st in ("resolved", "pending"):
            d = self._u(status=st)
            self.assertTrue(d.ok)
            self.assertFalse(d.data["needs_handoff"])

    def test_reopen_closed_needs_handoff(self):
        d = self._u(ticket={**TICKET, "status": "closed"}, status="open", priority="high")
        self.assertTrue(d.ok)
        self.assertTrue(d.data["needs_handoff"])

    def test_closed_cannot_become_resolved_or_change_priority_only(self):
        closed = {**TICKET, "status": "closed"}
        self.assertEqual(self._u(ticket=closed, status="resolved").code, "closed_can_only_reopen")
        self.assertEqual(self._u(ticket=closed, status=None, priority="high").code, "closed_can_only_reopen")

    def test_priority_raise_on_open_ticket_needs_handoff(self):
        d = self._u(status=None, priority="high")
        self.assertTrue(d.ok and d.data["needs_handoff"])

    def test_not_in_mail_or_not_owner_or_missing(self):
        for kw in ({"ids": ()}, {"ticket": None}, {"ticket": {**TICKET, "requester": "other@customer.test"}}):
            d = self._u(**kw)
            self.assertEqual(d.code, "ticket_not_found", kw)

    def test_bad_values(self):
        self.assertEqual(self._u(status=None, priority=None).code, "nothing_to_update")
        self.assertEqual(self._u(status="done").code, "invalid_status")
        self.assertEqual(self._u(status="open", priority="max").code, "invalid_priority")

    def test_get_ticket_hides_other_people_tickets(self):
        other = {**TICKET, "requester": "x@partner.test"}
        self.assertEqual(P.check_get_ticket(mk(), ["TKT-0001"], "TKT-0001", other).code, "ticket_not_found")
        self.assertTrue(P.check_get_ticket(mk(), ["TKT-0001"], "TKT-0001", TICKET).ok)


class ReplyRouteTests(unittest.TestCase):
    def test_reply_ok_returns_recipient(self):
        d = P.check_reply(state_for(2), mk(), 0, 20)
        self.assertTrue(d.ok)
        self.assertEqual(d.data["recipient"], "customer1@customer.test")

    def test_reply_blockers_and_escalation(self):
        d = P.check_reply(state_for(2), mk(auto_submitted="auto-replied"), 0, 20)
        self.assertEqual((d.code, d.escalate), ("auto_submitted", False))
        d = P.check_reply(state_for(2), mk(return_path="x@customer.test"), 0, 20)
        self.assertEqual((d.code, d.escalate), ("return_path_mismatch", True))

    def test_daily_cap(self):
        self.assertTrue(P.check_reply(state_for(2), mk(), 19, 20).ok)
        d = P.check_reply(state_for(2), mk(), 20, 20)
        self.assertEqual((d.code, d.escalate), ("recipient_daily_cap", True))

    def test_handoff_must_precede_reply(self):
        s = state_for(3)
        s.record("create_ticket", needs_handoff=True)
        self.assertEqual(P.check_reply(s, mk(), 0, 20).code, "handoff_first")
        self.assertTrue(P.check_route(s, mk(), "handoff", "alice@corp.test").ok)
        s.record("handoff")
        self.assertTrue(P.check_reply(s, mk(), 0, 20).ok)

    def test_route_rules(self):
        self.assertEqual(P.check_route(state_for(3), mk(), "forward", "a@corp.test").code, "invalid_target")
        d = P.check_route(state_for(5), mk(), "handoff", None)
        self.assertEqual((d.code, d.escalate), ("no_handoff_target", True))
        self.assertTrue(P.check_route(state_for(6), mk(), "archive", None).ok)
        self.assertEqual(P.check_route(state_for(3), mk(), "archive", None).code, "action_not_allowed")
        self.assertEqual(P.check_route(state_for(6), mk(), "handoff", "a@corp.test").code, "action_not_allowed")


class ObligationTests(unittest.TestCase):
    def test_by_type(self):
        m = mk()
        self.assertEqual(P.obligations(P.MessageState(), m), ["set_task_type"])
        self.assertEqual(P.obligations(state_for(1), m), [])
        self.assertEqual(P.obligations(state_for(2), m), ["reply"])
        self.assertEqual(P.obligations(state_for(3), m), ["create_ticket", "handoff", "reply"])
        self.assertEqual(P.obligations(state_for(4), m), ["update_ticket"])
        self.assertEqual(P.obligations(state_for(5), m), ["handoff"])
        self.assertEqual(P.obligations(state_for(6), m), ["archive"])
        self.assertEqual(P.obligations(state_for(8), m), ["create_ticket", "handoff", "reply"])

    def test_reply_not_demanded_when_blocked(self):
        m = mk(auto_submitted="auto-replied")
        self.assertEqual(P.obligations(state_for(2), m), [])

    def test_type4_reopen_walkthrough_m12(self):
        m = mk(subject="Về TKT-0003: lỗi vẫn còn")
        closed = {**TICKET, "id": "TKT-0003", "status": "closed"}
        s = state_for(4)
        d = P.check_update_ticket(s, m, ["TKT-0003"], "TKT-0003", closed, "open", "high")
        self.assertTrue(d.ok)
        s.record("update_ticket", needs_handoff=d.data["needs_handoff"])
        self.assertEqual(P.obligations(s, m), ["handoff"])
        self.assertEqual(P.check_reply(s, m, 0, 20).code, "handoff_first")
        s.record("handoff")
        self.assertEqual(P.obligations(s, m), [])

    def test_record_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            P.MessageState().record("delete_everything")


if __name__ == "__main__":
    unittest.main()
