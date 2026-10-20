"""Kiểm thử giai đoạn 0. Chạy: python -m unittest discover -s tests -v"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hma import db, kb
from hma.cli import main
from hma.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
REAL_KB = ROOT / "data" / "kb"
CFG_TEXT = (ROOT / "config" / "agent.yaml").read_text(encoding="utf-8")


def make_project(tmp: Path, cfg_text: str = CFG_TEXT, with_kb: bool = True) -> Path:
    (tmp / "config").mkdir()
    (tmp / "config" / "agent.yaml").write_text(cfg_text, encoding="utf-8")
    if with_kb:
        shutil.copytree(REAL_KB, tmp / "data" / "kb")
    return tmp / "config" / "agent.yaml"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_valid_config(self):
        cfg = load_config(make_project(self.tmp))
        self.assertEqual(cfg.handoff["client"], "alice@corp.test")
        self.assertEqual(cfg.sender_classes["partner.test"], "partner")
        self.assertEqual(cfg.paths.db, (self.tmp / "data" / "agent.db").resolve())
        self.assertEqual(cfg.kb_always_load, ("KB-015", "KB-018"))

    def test_missing_key(self):
        bad = CFG_TEXT.replace("  max_steps: 8\n", "")
        with self.assertRaisesRegex(ConfigError, "agent.max_steps"):
            load_config(make_project(self.tmp, bad))

    def test_bool_is_not_int(self):
        bad = CFG_TEXT.replace("max_steps: 8", "max_steps: true")
        with self.assertRaisesRegex(ConfigError, "max_steps"):
            load_config(make_project(self.tmp, bad))

    def test_non_positive(self):
        bad = CFG_TEXT.replace("kb_top_k: 3", "kb_top_k: 0")
        with self.assertRaisesRegex(ConfigError, "kb_top_k"):
            load_config(make_project(self.tmp, bad))

    def test_handoff_must_cover_classes(self):
        bad = CFG_TEXT.replace("  partner: bob@corp.test\n", "")
        with self.assertRaisesRegex(ConfigError, "thiếu người nhận"):
            load_config(make_project(self.tmp, bad))

    def test_uppercase_domain_rejected(self):
        bad = CFG_TEXT.replace("customer.test: client", "Customer.test: client")
        with self.assertRaisesRegex(ConfigError, "domain"):
            load_config(make_project(self.tmp, bad))

    def test_yaml_error_and_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(make_project(self.tmp, "llm: [unclosed"))
        with self.assertRaises(ConfigError):
            load_config(self.tmp / "nope.yaml")

    def test_unsafe_yaml_tag_rejected(self):
        bad = "x: !!python/object/apply:os.system ['echo hi']\n"
        with self.assertRaises(ConfigError):
            load_config(make_project(self.tmp, bad))


class DbTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.path = Path(self._t.name) / "sub" / "agent.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self._t.cleanup()

    def _msg(self, uid=1):
        key = db.make_message_key("INBOX", 111, uid)
        now = db.now_iso()
        self.conn.execute(
            "INSERT INTO messages(message_key,mailbox,uidvalidity,uid,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (key, "INBOX", 111, uid, "NEW", now, now))
        return key

    def test_seed_tickets(self):
        rows = self.conn.execute("SELECT id,status,priority FROM tickets ORDER BY id").fetchall()
        self.assertEqual([(r["id"], r["status"]) for r in rows],
                         [("TKT-0001", "open"), ("TKT-0002", "open"), ("TKT-0003", "closed")])

    def test_next_ticket_id_sequence(self):
        with db.transaction(self.conn):
            tid = db.next_ticket_id(self.conn)
            self.assertEqual(tid, "TKT-0004")
            now = db.now_iso()
            self.conn.execute(
                "INSERT INTO tickets(id,subject,requester,status,priority,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?)", (tid, "s", "a@customer.test", "open", "low", now, now))
        with db.transaction(self.conn):
            self.assertEqual(db.next_ticket_id(self.conn), "TKT-0005")

    def test_next_ticket_id_requires_transaction(self):
        with self.assertRaises(RuntimeError):
            db.next_ticket_id(self.conn)

    def test_transaction_rolls_back(self):
        with self.assertRaises(ZeroDivisionError):
            with db.transaction(self.conn):
                self.conn.execute("UPDATE tickets SET status='closed' WHERE id='TKT-0001'")
                1 / 0
        st = self.conn.execute("SELECT status FROM tickets WHERE id='TKT-0001'").fetchone()[0]
        self.assertEqual(st, "open")

    def test_check_constraints(self):
        now = db.now_iso()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tickets(id,subject,requester,status,priority,created_at,updated_at)"
                " VALUES ('TKT-0009','s','r','bogus','low',?,?)", (now, now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO tickets(id,subject,requester,status,priority,created_at,updated_at)"
                " VALUES ('BAD-1','s','r','open','low',?,?)", (now, now))

    def test_foreign_key_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO actions(message_key,action_type,status,created_at)"
                " VALUES ('INBOX:1:99','reply','INTENT',?)", (db.now_iso(),))

    def test_action_unique_per_message(self):
        key = self._msg()
        ins = "INSERT INTO actions(message_key,action_type,status,created_at) VALUES (?,?,?,?)"
        self.conn.execute(ins, (key, "reply", "INTENT", db.now_iso()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(ins, (key, "reply", "INTENT", db.now_iso()))
        self.conn.execute(ins, (key, "handoff", "INTENT", db.now_iso()))

    def test_message_unique_triplet(self):
        self._msg(uid=5)
        with self.assertRaises(sqlite3.IntegrityError):
            self._msg(uid=5)

    def test_init_requires_force_and_force_rebuilds(self):
        with db.transaction(self.conn):
            self.conn.execute("UPDATE tickets SET status='closed' WHERE id='TKT-0001'")
        self.conn.close()
        with self.assertRaises(FileExistsError):
            db.init_db(self.path)
        db.init_db(self.path, force=True)
        self.conn = db.connect(self.path)
        st = self.conn.execute("SELECT status FROM tickets WHERE id='TKT-0001'").fetchone()[0]
        self.assertEqual(st, "open")
        db.check_schema(self.conn)


class ForceSafetyTests(unittest.TestCase):
    def test_force_refuses_to_delete_non_sqlite_file(self):
        with tempfile.TemporaryDirectory() as t:
            victim = Path(t) / "quan_trong.txt"
            victim.write_text("dữ liệu không phải SQLite", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "không phải tệp SQLite"):
                db.init_db(victim, force=True)
            self.assertTrue(victim.exists())


class KbTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.path = self.tmp / "agent.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.docs = kb.load_kb_dir(REAL_KB)
        kb.replace_kb(self.conn, self.docs, ("KB-015", "KB-018"))

    def tearDown(self):
        self.conn.close()
        self._t.cleanup()

    def test_load_twenty_docs(self):
        self.assertEqual(len(self.docs), 20)
        n = self.conn.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0]
        self.assertEqual(n, 20)

    def test_always_load(self):
        ids = [d["doc_id"] for d in kb.always_load_docs(self.conn)]
        self.assertEqual(ids, ["KB-015", "KB-018"])

    def test_unknown_always_load_rejected(self):
        with self.assertRaises(kb.KBError):
            kb.replace_kb(self.conn, self.docs, ("KB-999",))

    def test_reload_is_idempotent(self):
        kb.replace_kb(self.conn, self.docs, ("KB-015",))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0], 20)
        self.assertEqual(len(kb.always_load_docs(self.conn)), 1)

    def test_search_expected_documents(self):
        cases = {
            "Bộ phận hỗ trợ làm việc vào giờ nào": "KB-001",
            "quên mật khẩu cổng khách hàng, đặt lại thế nào": "KB-002",
            "hoàn tiền xử lý trong bao lâu": "KB-003",
            "không nhận được email xác thực": "KB-005",
            "tài khoản bị khóa": "KB-006",
            "lỗi 429 hạn mức API": "KB-010",
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                hits = kb.search(self.conn, q, 3)
                self.assertEqual(hits[0]["doc_id"], expected, [h["doc_id"] for h in hits])

    def test_diacritics_are_significant(self):
        # "tiền" (tiền bạc) không được khớp "tiên" (đầu tiên, ưu tiên): ưu tiên độ chính xác.
        ids = [h["doc_id"] for h in kb.search(self.conn, "hoàn tiền", 5)]
        self.assertEqual(ids, ["KB-003"])
        self.assertEqual(kb.search(self.conn, "hoan tien", 3), [])

    def test_search_top_k_and_empty(self):
        self.assertLessEqual(len(kb.search(self.conn, "hỗ trợ", 2)), 2)
        self.assertEqual(kb.search(self.conn, "   !!! ", 3), [])
        with self.assertRaises(ValueError):
            kb.search(self.conn, "x", 0)

    def test_fts_operators_in_query_are_inert(self):
        for q in ['"', "(", "AND OR NOT", "hoàn*", 'a" OR "b', "NEAR(a b)", "col:val", "-x"]:
            with self.subTest(q=q):
                kb.search(self.conn, q, 3)

    def test_build_match(self):
        self.assertIsNone(kb.build_match("!!!"))
        self.assertEqual(kb.build_match("Hoàn Tiền hoàn"), '"hoàn" OR "tiền"')

    def test_parse_errors(self):
        d = self.tmp / "kbbad"
        d.mkdir()
        (d / "KB-001.md").write_text("không có tiêu đề\nnội dung", encoding="utf-8")
        with self.assertRaises(kb.KBError):
            kb.load_kb_dir(d)
        (d / "KB-001.md").write_text("# KB-001: A\n\nx", encoding="utf-8")
        (d / "KB-001b.md").write_text("# KB-001: B\n\ny", encoding="utf-8")
        with self.assertRaisesRegex(kb.KBError, "trùng"):
            kb.load_kb_dir(d)
        with self.assertRaises(kb.KBError):
            kb.load_kb_dir(self.tmp / "khong_co")

    def test_mailto_normalized(self):
        d = self.tmp / "kbm"
        d.mkdir()
        (d / "KB-015.md").write_text(
            "# KB-015: R\n\nchuyển cho [alice@corp.test](mailto:alice@corp.test).", encoding="utf-8")
        doc = kb.load_kb_dir(d)[0]
        self.assertEqual(doc.body, "chuyển cho alice@corp.test.")


class CliTests(unittest.TestCase):
    def test_init_status_search_flow(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = make_project(Path(t))
            self.assertEqual(main(["--config", str(cfgp), "init-db"]), 0)
            self.assertEqual(main(["--config", str(cfgp), "init-db"]), 2)
            self.assertEqual(main(["--config", str(cfgp), "init-db", "--force"]), 0)
            self.assertEqual(main(["--config", str(cfgp), "status"]), 0)
            self.assertEqual(main(["--config", str(cfgp), "kb-search", "hoàn tiền"]), 0)

    def test_missing_kb_dir_is_clean_error(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = make_project(Path(t), with_kb=False)
            self.assertEqual(main(["--config", str(cfgp), "init-db"]), 2)

    def test_status_without_db_is_clean_error_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = make_project(Path(t))
            for cmd in (["status"], ["kb-search", "hoàn tiền"]):
                self.assertEqual(main(["--config", str(cfgp)] + cmd), 2)
            self.assertFalse((Path(t) / "data" / "agent.db").exists())

    def test_empty_db_file_left_by_failed_run_is_replaced(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = make_project(Path(t))
            dbp = Path(t) / "data" / "agent.db"
            dbp.write_bytes(b"")
            self.assertEqual(main(["--config", str(cfgp), "init-db"]), 0)
            self.assertGreater(dbp.stat().st_size, 0)

    def test_wrong_schema_version_is_db_error(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "old.db"
            raw = sqlite3.connect(str(p))
            raw.execute("CREATE TABLE x(a)")
            raw.commit()
            raw.close()
            conn = db.connect_existing(p)
            try:
                with self.assertRaises(db.DbError):
                    db.check_schema(conn)
            finally:
                conn.close()

    def test_force_on_non_sqlite_file_exits_2(self):
        with tempfile.TemporaryDirectory() as t:
            cfgp = make_project(Path(t))
            dbp = Path(t) / "data" / "agent.db"
            dbp.parent.mkdir(exist_ok=True)
            dbp.write_text("không phải sqlite", encoding="utf-8")
            self.assertEqual(main(["--config", str(cfgp), "init-db", "--force"]), 2)
            self.assertTrue(dbp.exists())


if __name__ == "__main__":
    unittest.main()
