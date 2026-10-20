"""2C: store ghi/đọc ghi chú; tất cả database nằm trong TemporaryDirectory."""
from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hma import db, store


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store-test.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.check_schema(self.conn)
        self.key = store.register_message(
            self.conn, mailbox="INBOX", uidvalidity=111, uid=7,
            sender="customer1@customer.test", sender_class="client", subject="Bổ sung ticket", truncated=False)
        self.partner_key = store.register_message(
            self.conn, mailbox="INBOX", uidvalidity=111, uid=8,
            sender="partner1@partner.test", sender_class="partner", subject="Thông tin đối tác", truncated=False)

    def args(self, **changes):
        values = dict(ticket_id="TKT-0001", body="Khách đã thử đăng nhập lại.",
                      source_message_key=self.key, run_id="functional-run-01", profile="protected")
        values.update(changes)
        return values

    def note(self, **changes):
        with db.transaction(self.conn):
            return store.append_ticket_note(self.conn, **self.args(**changes))

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]

    def core(self):
        return {t: [tuple(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in ("tickets", "messages", "actions", "llm_calls", "kb_docs", "kb_fts")}

    def test_append_returns_complete_internal_record(self):
        note = self.note()
        self.assertEqual(set(note), {"id", "ticket_id", "requester", "body", "source_message_key", "run_id", "profile", "created_at"})
        self.assertIsInstance(note["id"], int)
        self.assertEqual(note["requester"], "customer1@customer.test")
        self.assertEqual((note["source_message_key"], note["run_id"], note["profile"]),
                         (self.key, "functional-run-01", "protected"))
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-0001"), [note])

    def test_requester_is_ticket_owner_not_source_sender(self):
        note = self.note(source_message_key=self.partner_key)
        self.assertEqual(note["requester"], "customer1@customer.test")
        self.assertEqual(note["source_message_key"], self.partner_key)

    def test_both_profiles_share_storage_path(self):
        a = self.note(profile="vulnerable")
        b = self.note(profile="protected")
        self.assertEqual(set(a), set(b))
        self.assertEqual([r["profile"] for r in store.list_ticket_notes(self.conn, "TKT-0001")], ["protected", "vulnerable"])

    def test_body_is_preserved_without_semantic_filtering(self):
        body = "  Dòng 1\nDòng 2: cấu hình được nhắc trong email.  "
        self.assertEqual(self.note(body=body)["body"], body)

    def test_append_requires_existing_transaction(self):
        with self.assertRaises(RuntimeError):
            store.append_ticket_note(self.conn, **self.args())
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_note_rolls_back_with_caller(self):
        before = self.core()
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                store.append_ticket_note(self.conn, **self.args())
                raise RuntimeError("lỗi sau khi ghi")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.core(), before)

    def test_append_does_not_commit_callers_transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            store.append_ticket_note(self.conn, **self.args())
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.count(), 1)
        finally:
            self.conn.execute("ROLLBACK")
        self.assertEqual(self.count(), 0)

    def test_missing_ticket_is_rejected_without_creating_ticket(self):
        before = self.core()
        with self.assertRaisesRegex(ValueError, "ticket không tồn tại"):
            self.note(ticket_id="TKT-9999")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.core(), before)

    def test_missing_source_message_preserves_foreign_key_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.note(source_message_key="INBOX:111:999")
        self.assertEqual(self.count(), 0)

    def test_invalid_body_is_rejected(self):
        for value in ("", "  ", "\t\n", "x\x00y", 3, None, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.note(body=value)
        self.assertEqual(self.count(), 0)

    def test_oversized_body_is_rejected(self):
        with self.assertRaises(ValueError):
            self.note(body="x" * (store.NOTE_BODY_MAX_CHARS + 1))
        self.assertEqual(self.count(), 0)

    def test_body_limit_counts_unicode_characters(self):
        body = "ắ" * store.NOTE_BODY_MAX_CHARS
        self.assertEqual(self.note(body=body)["body"], body)

    def test_invalid_profile_is_rejected(self):
        for value in ("unknown", "", None, True, "protected\x00"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.note(profile=value)
        self.assertEqual(self.count(), 0)

    def test_invalid_run_id_is_rejected(self):
        for value in (None, "", "  ", "x\x00y", 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.note(run_id=value)
        self.assertEqual(self.count(), 0)

    def test_invalid_ticket_and_source_key_values_are_rejected(self):
        for value in (None, "", "  ", "x\x00y", 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.note(ticket_id=value)
                with self.assertRaises(ValueError):
                    self.note(source_message_key=value)
                with self.assertRaises(ValueError):
                    store.list_ticket_notes(self.conn, value)
        self.assertEqual(self.count(), 0)

    def test_invalid_read_limit_is_rejected(self):
        for limit in (0, -1, store.NOTE_READ_MAX_LIMIT + 1, True, 1.5, "10", None):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    store.list_ticket_notes(self.conn, "TKT-0001", limit=limit)

    def test_default_read_limit(self):
        notes = [self.note(body=f"Ghi chú {i}") for i in range(store.NOTE_READ_DEFAULT_LIMIT + 2)]
        got = store.list_ticket_notes(self.conn, "TKT-0001")
        self.assertEqual(len(got), store.NOTE_READ_DEFAULT_LIMIT)
        self.assertEqual([r["id"] for r in got], [r["id"] for r in reversed(notes)][:store.NOTE_READ_DEFAULT_LIMIT])

    def test_newest_first_even_when_timestamps_match(self):
        with patch("hma.store.now_iso", return_value="2026-10-16T19:10:00"):
            notes = [self.note(body=f"Ghi chú {i}") for i in range(3)]
        got = store.list_ticket_notes(self.conn, "TKT-0001")
        self.assertEqual([r["id"] for r in got], [r["id"] for r in reversed(notes)])
        self.assertEqual(len({r["created_at"] for r in got}), 1)

    def test_explicit_read_limits(self):
        self.note()
        self.note()
        self.assertEqual(len(store.list_ticket_notes(self.conn, "TKT-0001", limit=1)), 1)
        self.assertEqual(len(store.list_ticket_notes(self.conn, "TKT-0001", limit=store.NOTE_READ_MAX_LIMIT)), 2)

    def test_ticket_notes_do_not_mix(self):
        a = self.note()
        b = self.note(ticket_id="TKT-0002", source_message_key=self.partner_key)
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-0001"), [a])
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-0002"), [b])
        self.assertEqual(b["requester"], "partner1@partner.test")

    def test_empty_or_missing_ticket_returns_empty_list(self):
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-0001"), [])
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-9999"), [])

    def test_committed_note_survives_reopening_connection(self):
        note = self.note()
        self.conn.close()
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(store.list_ticket_notes(self.conn, "TKT-0001"), [note])

    def test_append_does_not_change_core_rows_or_ticket_sequence(self):
        before = self.core()
        self.note()
        self.assertEqual(self.core(), before)
        with db.transaction(self.conn):
            self.assertEqual(db.next_ticket_id(self.conn), "TKT-0004")

    def test_foreign_key_error_is_not_unique_violation(self):
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.note(source_message_key="INBOX:111:999")
        self.assertFalse(store.is_unique_violation(caught.exception))

    def test_read_does_not_start_transaction_or_write(self):
        self.note()
        before = self.conn.total_changes
        self.assertFalse(self.conn.in_transaction)
        store.list_ticket_notes(self.conn, "TKT-0001")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, before)

    def test_requester_is_not_a_caller_argument(self):
        self.assertNotIn("requester", inspect.signature(store.append_ticket_note).parameters)
        with db.transaction(self.conn):
            with self.assertRaises(TypeError):
                store.append_ticket_note(self.conn, requester="other@customer.test", **self.args())
        self.assertEqual(self.count(), 0)

    def test_multiple_explicit_notes_for_same_email_are_allowed(self):
        a = self.note(body="Thông tin thứ nhất")
        b = self.note(body="Thông tin thứ hai")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.count(), 2)

    def test_limit_above_available_rows_returns_all_rows(self):
        notes = [self.note(body=f"Ghi chú {i}") for i in range(3)]
        got = store.list_ticket_notes(self.conn, "TKT-0001", limit=store.NOTE_READ_MAX_LIMIT)
        self.assertEqual(got, list(reversed(notes)))


if __name__ == "__main__":
    unittest.main()
