"""14 kiểm tra migration memory; dùng fixture v1 và database tạm."""
from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path
from hma import db
from hma.memory_schema import migrate_ticket_memory


class MemorySchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'memory-test.db'
        legacy = Path(__file__).resolve().parent / 'fixtures' / 'hma_schema_v1.sql'
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.executescript(legacy.read_text(encoding='utf-8'))
        with db.transaction(self.conn):
            self.conn.executemany(
                'INSERT INTO tickets(id,subject,requester,status,priority,assignee,'
                'source_message_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)', db.SEED_TICKETS)
        self.assertEqual(self.version(), 1)
        now = db.now_iso()
        self.key = db.make_message_key('INBOX', 111, 7)
        self.conn.execute(
            'INSERT INTO messages(message_key,mailbox,uidvalidity,uid,sender,status,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (self.key, 'INBOX', 111, 7, 'customer1@customer.test', 'DONE', now, now))
        self.conn.execute('INSERT INTO actions(message_key,action_type,target,status,created_at) VALUES (?,?,?,?,?)',
                          (self.key, 'reply', 'customer1@customer.test', 'SENT', now))
        self.conn.execute('INSERT INTO llm_calls(message_key,step,total_tokens,created_at) VALUES (?,?,?,?)',
                          (self.key, 1, 30, now))
        self.conn.execute("INSERT INTO kb_docs(doc_id,title,body) VALUES ('KB-X','Tiêu đề','Nội dung')")
        self.conn.execute("INSERT INTO kb_fts(doc_id,title,body) VALUES ('KB-X','Tiêu đề','Nội dung')")
        self.conn.execute("UPDATE tickets SET priority='high' WHERE id='TKT-0001'")

    def version(self):
        return self.conn.execute('PRAGMA user_version').fetchone()[0]

    def base_rows(self):
        return {t: [tuple(r) for r in self.conn.execute(f'SELECT * FROM {t} ORDER BY rowid')]
                for t in ('messages','tickets','actions','llm_calls','kb_docs','kb_fts')}

    def add_note(self, **changes):
        v = dict(ticket_id='TKT-0001', requester='customer1@customer.test', body='Khách đã thử lại.',
                 source_message_key=self.key, run_id='functional-run-01', profile='protected', created_at=db.now_iso())
        v.update(changes)
        cur = self.conn.execute(f"INSERT INTO ticket_notes({','.join(v)}) VALUES ({','.join('?' for _ in v)})",
                                tuple(v.values()))
        return int(cur.lastrowid)

    def test_migration_preserves_all_base_rows_and_ticket_sequence(self):
        before = self.base_rows()
        self.assertEqual(migrate_ticket_memory(self.conn), 2)
        self.assertEqual(self.version(), 2)
        self.assertEqual(self.base_rows(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 0)
        with db.transaction(self.conn):
            self.assertEqual(db.next_ticket_id(self.conn), 'TKT-0004')
        self.assertFalse(self.conn.in_transaction)

    def test_second_migration_preserves_note_and_base_rows(self):
        migrate_ticket_memory(self.conn)
        self.add_note()
        before = self.base_rows()
        notes = [tuple(r) for r in self.conn.execute('SELECT * FROM ticket_notes')]
        self.assertEqual(migrate_ticket_memory(self.conn), 2)
        self.assertEqual(self.base_rows(), before)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT * FROM ticket_notes')], notes)

    def test_ddl_failure_rolls_back_table_and_version(self):
        self.conn.execute('CREATE INDEX idx_ticket_notes_ticket_id_id ON tickets(id)')
        before = self.base_rows()
        with self.assertRaises(sqlite3.OperationalError):
            migrate_ticket_memory(self.conn)
        self.assertEqual(self.version(), 1)
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ticket_notes'").fetchone())
        self.assertEqual(self.base_rows(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_unsupported_versions_are_rejected(self):
        for version in (0, 3):
            with self.subTest(version=version):
                self.conn.execute(f'PRAGMA user_version={version}')
                before = self.base_rows()
                with self.assertRaises(db.DbError):
                    migrate_ticket_memory(self.conn)
                self.assertEqual(self.version(), version)
                self.assertEqual(self.base_rows(), before)
                self.assertFalse(self.conn.in_transaction)

    def test_fake_v1_without_hma_tables_is_rejected(self):
        conn = db.connect(':memory:')
        try:
            conn.execute('PRAGMA user_version=1')
            with self.assertRaises(db.DbError):
                migrate_ticket_memory(conn)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
        finally:
            conn.close()

    def test_v2_missing_index_is_not_silently_repaired(self):
        migrate_ticket_memory(self.conn)
        self.conn.execute('DROP INDEX idx_ticket_notes_ticket_id_id')
        with self.assertRaises(db.DbError):
            migrate_ticket_memory(self.conn)
        self.assertEqual(self.version(), 2)

    def test_refuses_call_inside_existing_transaction(self):
        with db.transaction(self.conn):
            with self.assertRaises(db.DbError):
                migrate_ticket_memory(self.conn)
            self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.version(), 1)

    def test_requires_foreign_keys_enabled(self):
        self.conn.execute('PRAGMA foreign_keys=OFF')
        with self.assertRaises(db.DbError):
            migrate_ticket_memory(self.conn)
        self.assertEqual(self.version(), 1)

    def test_note_foreign_keys_are_enforced(self):
        migrate_ticket_memory(self.conn)
        for changes in ({'ticket_id':'TKT-9999'}, {'source_message_key':'INBOX:111:999'}):
            with self.subTest(changes=changes):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.add_note(**changes)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 0)

    def test_note_constraints_and_both_profiles(self):
        migrate_ticket_memory(self.conn)
        for changes in ({'body':'   '}, {'requester':''}, {'run_id':''}, {'profile':'unknown'}):
            with self.subTest(changes=changes):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.add_note(**changes)
        self.add_note(profile='vulnerable')
        self.add_note(profile='protected')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 2)

    def test_note_survives_reopening_connection(self):
        migrate_ticket_memory(self.conn)
        note_id = self.add_note()
        self.conn.close()
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        row = self.conn.execute('SELECT * FROM ticket_notes WHERE id=?',(note_id,)).fetchone()
        self.assertEqual((row['ticket_id'],row['source_message_key'],row['run_id'],row['profile']),
                         ('TKT-0001',self.key,'functional-run-01','protected'))

    def test_note_insert_rolls_back_with_caller_transaction(self):
        migrate_ticket_memory(self.conn)
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                self.add_note()
                raise RuntimeError('mô phỏng lỗi nghiệp vụ')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 0)

    def test_read_order_is_deterministic_when_timestamps_match(self):
        migrate_ticket_memory(self.conn)
        ids = [self.add_note(body=f'Ghi chú {i}',created_at='2026-10-16T18:44:00') for i in range(3)]
        rows = self.conn.execute('SELECT id FROM ticket_notes WHERE ticket_id=? ORDER BY id DESC LIMIT ?',('TKT-0001',2)).fetchall()
        self.assertEqual([r['id'] for r in rows],list(reversed(ids))[:2])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes WHERE ticket_id=?',('TKT-0002',)).fetchone()[0],0)

    def test_runtime_requires_v2_and_accepts_migrated_database(self):
        self.assertEqual(db.SCHEMA_VERSION,2)
        with self.assertRaisesRegex(db.DbError,'migrate-db'):
            db.check_schema(self.conn)
        migrate_ticket_memory(self.conn)
        db.check_schema(self.conn)


if __name__ == '__main__':
    unittest.main()
