"""Kiểm thử giai đoạn 1 (không cần mạng). Chạy: python -m unittest discover -s tests -v"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hma.mailio import (ImapSession, MailboxInfo, MailError, build_message, header_summary,
                        make_ssl_context, parse_list_line, quote_mailbox)


class ParseListTests(unittest.TestCase):
    def test_plain_and_quoted(self):
        self.assertEqual(parse_list_line(b'(\\HasNoChildren) "." INBOX'),
                         ("INBOX", ".", ("\\HasNoChildren",)))
        self.assertEqual(parse_list_line(b'(\\HasNoChildren \\Archive) "/" "Archive"'),
                         ("Archive", "/", ("\\HasNoChildren", "\\Archive")))

    def test_nil_delimiter_and_garbage(self):
        self.assertEqual(parse_list_line(b'() NIL Shared')[:2], ("Shared", ""))
        self.assertIsNone(parse_list_line("không phải dòng LIST".encode("utf-8")))


class HeaderSummaryTests(unittest.TestCase):
    def test_full_headers(self):
        raw = (b"Return-Path: <customer1@customer.test>\r\nFrom: =?utf-8?q?Kh=C3=A1ch?= <Customer1@Customer.test>\r\n"
               b"Subject: =?utf-8?q?C=E1=BA=A3m_=C6=A1n?=\r\nMessage-ID: <a@b>\r\nAuto-Submitted: auto-replied\r\n"
               b"List-Id: <x.y>\r\nPrecedence: bulk\r\n\r\n")
        h = header_summary(raw)
        self.assertEqual(h["from"], "customer1@customer.test")
        self.assertEqual(h["return_path"], "customer1@customer.test")
        self.assertTrue(h["return_path_present"] and h["has_list_header"])
        self.assertEqual(h["auto_submitted"], "auto-replied")
        self.assertEqual(h["precedence"], "bulk")
        self.assertEqual(h["subject"], "Cảm ơn")

    def test_missing_and_null_return_path(self):
        h = header_summary(b"From: a@b.test\r\n\r\n")
        self.assertFalse(h["return_path_present"])
        self.assertIsNone(h["return_path"])
        self.assertIsNone(h["message_id"])
        h = header_summary(b"Return-Path: <>\r\nFrom: a@b.test\r\n\r\n")
        self.assertTrue(h["return_path_present"])
        self.assertEqual(h["return_path"], "")


class BuildMessageTests(unittest.TestCase):
    def test_basic(self):
        m = build_message(from_addr="support@corp.test", to_addrs=["alice@corp.test"],
                          subject="Chào", body="Nội dung tiếng Việt",
                          extra_headers={"Auto-Submitted": "auto-replied"})
        self.assertEqual(m["To"], "alice@corp.test")
        self.assertTrue(str(m["Message-ID"]).endswith("@corp.test>"))
        self.assertEqual(m["Auto-Submitted"], "auto-replied")
        self.assertIn("Nội dung tiếng Việt", m.get_content())
        m.as_bytes()  # phải serialize được

    def test_header_injection_rejected(self):
        for kw in ({"subject": "a\r\nBcc: x@evil.test"}, {"subject": "a\nBcc: x@evil.test"}):
            with self.assertRaises(ValueError):
                build_message(from_addr="support@corp.test", to_addrs=["alice@corp.test"], body="x", **kw)
        with self.assertRaises(ValueError):
            build_message(from_addr="support@corp.test", to_addrs=["alice@corp.test"], subject="s",
                          body="x", extra_headers={"Reply-To": "a@b.test\r\nBcc: x@evil.test"})

    def test_requires_recipient(self):
        with self.assertRaises(ValueError):
            build_message(from_addr="support@corp.test", to_addrs=[], subject="s", body="x")


class QuoteMailboxTests(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(quote_mailbox("Archive"), '"Archive"')
        self.assertEqual(quote_mailbox('a"b'), '"a\\"b"')
        for bad in ("a\r\nb", "x\x00y"):
            with self.assertRaises(ValueError):
                quote_mailbox(bad)


class SslContextTests(unittest.TestCase):
    def test_missing_and_garbage_ca(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(MailError, "không thấy"):
                make_ssl_context(Path(t) / "nope.crt")
            bad = Path(t) / "bad.crt"
            bad.write_text("không phải chứng chỉ", encoding="utf-8")
            with self.assertRaises(MailError):
                make_ssl_context(bad)


class MoveGuardTests(unittest.TestCase):
    def _session(self, readonly):
        s = ImapSession("localhost", 143, "u", "p", ssl_context=None)  # type: ignore[arg-type]
        s.selected = MailboxInfo("INBOX", 1, 1, readonly)
        return s

    def test_refuses_readonly(self):
        with self.assertRaisesRegex(MailError, "chế độ ghi"):
            self._session(True).move(1, "Archive", frozenset({"MOVE"}))

    def test_refuses_without_move_or_uidplus(self):
        with self.assertRaisesRegex(MailError, "MOVE lẫn UIDPLUS"):
            self._session(False).move(1, "Archive", frozenset({"IMAP4REV1"}))

    def test_refuses_when_nothing_selected(self):
        s = ImapSession("localhost", 143, "u", "p", ssl_context=None)  # type: ignore[arg-type]
        with self.assertRaises(MailError):
            s.move(1, "Archive", frozenset({"MOVE"}))


if __name__ == "__main__":
    unittest.main()
