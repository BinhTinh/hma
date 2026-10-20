"""Tích hợp schema v2 và CLI migration. Tất cả database nằm trong thư mục tạm."""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from hma import db
from hma.memory_schema import migrate_ticket_memory

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / 'tests' / 'fixtures' / 'hma_schema_v1.sql'


def init_legacy(path):
    conn = db.connect(path)
    try:
        conn.executescript(LEGACY.read_text(encoding='utf-8'))
        with db.transaction(conn):
            conn.executemany(
                'INSERT INTO tickets(id,subject,requester,status,priority,assignee,'
                'source_message_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                db.SEED_TICKETS)
    finally:
        conn.close()


class SchemaIntegrationTests(unittest.TestCase):
    def test_fresh_database_is_v2_with_empty_memory_and_original_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'new.db'
            db.init_db(path)
            conn = db.connect(path)
            try:
                db.check_schema(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 3)
                self.assertEqual(migrate_ticket_memory(conn), 2)
            finally:
                conn.close()

    def test_fresh_and_migrated_memory_definitions_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh, old = Path(tmp) / 'fresh.db', Path(tmp) / 'old.db'
            db.init_db(fresh)
            init_legacy(old)
            definitions = []
            for path in (fresh, old):
                conn = db.connect(path)
                try:
                    migrate_ticket_memory(conn)
                    definitions.append([tuple(r) for r in conn.execute(
                        "SELECT name,sql FROM sqlite_master WHERE name IN "
                        "('ticket_notes','idx_ticket_notes_ticket_id_id') ORDER BY name")])
                finally:
                    conn.close()
            self.assertEqual(definitions[0], definitions[1])

    def test_check_schema_v1_refuses_without_migrating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.db'
            init_legacy(path)
            conn = db.connect(path)
            try:
                with self.assertRaisesRegex(db.DbError, 'migrate-db'):
                    db.check_schema(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='ticket_notes'").fetchone())
            finally:
                conn.close()

    def test_check_schema_unknown_version_refuses_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unknown.db'
            db.init_db(path)
            conn = db.connect(path)
            try:
                conn.execute('PRAGMA user_version=3')
                with self.assertRaises(db.DbError):
                    db.check_schema(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            finally:
                conn.close()


class CliMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'data').mkdir()
        self.cfg = self.root / 'config' / 'agent.yaml'
        self.cfg.write_text((ROOT / 'config' / 'agent.yaml').read_text(encoding='utf-8'), encoding='utf-8')
        self.path = self.root / 'data' / 'agent.db'

    def call_cli(self, *args):
        from hma.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--config', str(self.cfg), *args])
        return code, out.getvalue(), err.getvalue()

    def test_migrate_preserves_ticket_and_kb_and_can_repeat(self):
        init_legacy(self.path)
        conn = db.connect(self.path)
        try:
            conn.execute("UPDATE tickets SET priority='high' WHERE id='TKT-0001'")
            conn.execute("INSERT INTO kb_docs(doc_id,title,body) VALUES ('KEEP','T','B')")
            conn.execute("INSERT INTO kb_fts(doc_id,title,body) VALUES ('KEEP','T','B')")
            before = [tuple(r) for r in conn.execute('SELECT * FROM tickets ORDER BY id')]
        finally:
            conn.close()
        code, out, err = self.call_cli('migrate-db')
        self.assertEqual(code, 0, err)
        self.assertIn('v1 -> v2', out)
        code, out, err = self.call_cli('migrate-db')
        self.assertEqual(code, 0, err)
        self.assertIn('không thay đổi dữ liệu', out)
        conn = db.connect(self.path)
        try:
            db.check_schema(conn)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM tickets ORDER BY id')], before)
            self.assertEqual(tuple(conn.execute("SELECT title,body FROM kb_docs WHERE doc_id='KEEP'").fetchone()), ('T', 'B'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM kb_fts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0], 0)
        finally:
            conn.close()

    def test_missing_database_is_not_created(self):
        code, out, err = self.call_cli('migrate-db')
        self.assertEqual(code, 2, (out, err))
        self.assertFalse(self.path.exists())

    def test_unsupported_version_is_not_changed(self):
        init_legacy(self.path)
        conn = db.connect(self.path)
        conn.execute('PRAGMA user_version=3')
        conn.close()
        code, out, err = self.call_cli('migrate-db')
        self.assertEqual(code, 2, (out, err))
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
        finally:
            conn.close()

    def test_init_and_status_use_v2(self):
        shutil.copytree(ROOT / 'data' / 'kb', self.root / 'data' / 'kb')
        code, out, err = self.call_cli('init-db')
        self.assertEqual(code, 0, (out, err))
        code, out, err = self.call_cli('status')
        self.assertEqual(code, 0, (out, err))
        self.assertIn('ticket_notes: 0', out)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
        finally:
            conn.close()

    def test_malformed_v2_is_rejected_without_silent_repair(self):
        db.init_db(self.path)
        conn = db.connect(self.path)
        conn.execute('DROP INDEX idx_ticket_notes_ticket_id_id')
        conn.close()
        code, out, err = self.call_cli('migrate-db')
        self.assertEqual(code, 2, (out, err))
        conn = db.connect(self.path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='idx_ticket_notes_ticket_id_id'").fetchone())
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
