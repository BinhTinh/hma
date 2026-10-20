"""SQLite: lược đồ, kết nối, giao dịch, sinh mã ticket (giai đoạn 0)."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .memory_schema import (
    MEMORY_SCHEMA_VERSION, TICKET_NOTES_TABLE_SQL, TICKET_NOTES_INDEX_SQL,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = MEMORY_SCHEMA_VERSION

SCHEMA = f"""
PRAGMA user_version = {SCHEMA_VERSION};

CREATE TABLE messages (
  message_key   TEXT PRIMARY KEY,
  mailbox       TEXT NOT NULL,
  uidvalidity   INTEGER NOT NULL,
  uid           INTEGER NOT NULL,
  sender        TEXT,
  sender_class  TEXT CHECK (sender_class IS NULL OR sender_class IN ('client','partner')),
  subject       TEXT,
  task_type     INTEGER CHECK (task_type IS NULL OR task_type IN (1,2,3,4,5,6,8)),
  status        TEXT NOT NULL CHECK (status IN
                  ('NEW','CLAIMED','DONE','NEEDS_HUMAN','FAILED','SKIPPED')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  handoff_status TEXT NOT NULL DEFAULT 'NONE'
                  CHECK (handoff_status IN ('NONE','SENT','FAILED')),
  reason        TEXT,
  truncated     INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),
  claimed_at    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (mailbox, uidvalidity, uid)
);

CREATE TABLE tickets (
  id                 TEXT PRIMARY KEY CHECK (id GLOB 'TKT-[0-9][0-9][0-9][0-9]'),
  subject            TEXT NOT NULL,
  requester          TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('open','pending','resolved','closed')),
  priority           TEXT NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
  assignee           TEXT,
  source_message_key TEXT REFERENCES messages(message_key),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  message_key TEXT NOT NULL REFERENCES messages(message_key),
  action_type TEXT NOT NULL CHECK (action_type IN
                ('create_ticket','update_ticket','reply','handoff','archive')),
  target      TEXT,
  status      TEXT NOT NULL CHECK (status IN ('INTENT','SENT','DONE','FAILED')),
  detail      TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (message_key, action_type)
);

CREATE TABLE llm_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  message_key       TEXT NOT NULL REFERENCES messages(message_key),
  step              INTEGER NOT NULL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  reasoning_tokens  INTEGER,
  cache_hit_tokens  INTEGER,
  cache_miss_tokens INTEGER,
  latency_ms        INTEGER,
  finish_reason     TEXT,
  created_at        TEXT NOT NULL
);

CREATE TABLE kb_docs (
  doc_id      TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  always_load INTEGER NOT NULL DEFAULT 0 CHECK (always_load IN (0,1))
);

CREATE VIRTUAL TABLE kb_fts USING fts5(
  doc_id UNINDEXED, title, body,
  tokenize = 'unicode61 remove_diacritics 0'
);
"""

SCHEMA += "\n" + TICKET_NOTES_TABLE_SQL + ";\n" + TICKET_NOTES_INDEX_SQL + ";\n"

SEED_TICKETS = [
    ("TKT-0001", "Lỗi đăng nhập cổng khách hàng", "customer1@customer.test",
     "open", "normal", None, None, "2026-10-01T09:00:00", "2026-10-01T09:00:00"),
    ("TKT-0002", "Lỗi chữ ký webhook", "partner1@partner.test",
     "open", "normal", None, None, "2026-10-01T10:00:00", "2026-10-01T10:00:00"),
    ("TKT-0003", "Hóa đơn sai số tiền", "customer1@customer.test",
     "closed", "normal", None, None, "2026-09-28T08:00:00", "2026-09-30T16:00:00"),
]


class DbError(RuntimeError):
    """CSDL chưa có hoặc sai phiên bản lược đồ."""


def now_iso() -> str:
    """Thời gian cục bộ, không kèm múi giờ (cùng định dạng với dữ liệu seed)."""
    return datetime.now().replace(microsecond=0).isoformat()


def make_message_key(mailbox: str, uidvalidity: int, uid: int) -> str:
    """Khóa thư = (hộp, UIDVALIDITY, UID), theo RFC 3501."""
    return f"{mailbox}:{uidvalidity}:{uid}"


def connect(path: str | Path) -> sqlite3.Connection:
    """Mở kết nối: autocommit (isolation_level=None), bật khóa ngoại, row dạng dict."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_existing(path: str | Path) -> sqlite3.Connection:
    """Mở CSDL đã có. Không tự tạo tệp mới."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        raise DbError(f"chưa có CSDL tại {p}; hãy chạy 'python -m hma init-db' trước")
    return connect(p)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; lỗi thì ROLLBACK rồi ném lại."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_ticket_id(conn: sqlite3.Connection) -> str:
    """Mã ticket kế tiếp. PHẢI gọi trong transaction() cùng lúc với INSERT ticket."""
    if not conn.in_transaction:
        raise RuntimeError("next_ticket_id phải được gọi trong transaction()")
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(id, 5) AS INTEGER)), 0) + 1 AS n FROM tickets"
    ).fetchone()
    n = int(row["n"])
    if n > 9999:
        raise RuntimeError("hết dải mã TKT-0001..TKT-9999")
    return f"TKT-{n:04d}"


def check_schema(conn: sqlite3.Connection) -> None:
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver != SCHEMA_VERSION:
        if ver == 1:
            raise DbError(
                f"phiên bản lược đồ {ver} != {SCHEMA_VERSION}; "
                "hãy sao lưu rồi chạy 'python -m hma migrate-db'; "
                "không dùng init-db --force để nâng cấp")
        raise DbError(
            f"phiên bản lược đồ {ver} không được hỗ trợ; cần {SCHEMA_VERSION}; "
            "không tự dựng lại database")


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _remove_db_files(path: Path) -> None:
    """Chỉ xóa database cũ nếu đúng là tệp SQLite."""
    if path.exists():
        with path.open("rb") as fh:
            head = fh.read(16)
        if path.stat().st_size > 0 and head != _SQLITE_MAGIC:
            raise ValueError(f"{path} không phải tệp SQLite; từ chối xóa")
    for suffix in ("", "-wal", "-shm", "-journal"):
        f = Path(str(path) + suffix)
        if f.exists():
            f.unlink()


def init_db(path: str | Path, *, force: bool = False) -> None:
    """Tạo CSDL mới với lược đồ + 3 ticket seed. KB nạp riêng bằng kb.replace_kb."""
    p = Path(path)
    if p.is_file() and p.stat().st_size == 0:
        p.unlink()
    if p.exists() and not force:
        raise FileExistsError(f"{p} đã tồn tại; dùng --force để xóa và dựng lại")
    p.parent.mkdir(parents=True, exist_ok=True)
    if force:
        _remove_db_files(p)
    conn = connect(p)
    try:
        conn.executescript(SCHEMA)
        with transaction(conn):
            conn.executemany(
                "INSERT INTO tickets(id, subject, requester, status, priority, assignee,"
                " source_message_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                SEED_TICKETS,
            )
    finally:
        conn.close()
    log.info("đã khởi tạo CSDL %s (lược đồ v%d, %d ticket seed)", p, SCHEMA_VERSION, len(SEED_TICKETS))
