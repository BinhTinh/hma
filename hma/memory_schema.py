"""Schema memory ticket v2; chưa tự kích hoạt trong runtime.

DDL được dùng lại cho khởi tạo database mới ở bước tích hợp tiếp theo.
Migration chỉ được gọi tường minh, không được gọi từ đường xử lý email.
"""
from __future__ import annotations

import sqlite3

MEMORY_SCHEMA_VERSION = 2

TICKET_NOTES_TABLE_SQL = """
CREATE TABLE ticket_notes (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id          TEXT NOT NULL REFERENCES tickets(id),
  requester          TEXT NOT NULL CHECK (length(trim(requester)) > 0),
  body               TEXT NOT NULL CHECK (length(trim(body)) > 0),
  source_message_key TEXT NOT NULL REFERENCES messages(message_key),
  run_id             TEXT NOT NULL CHECK (length(trim(run_id)) > 0),
  profile            TEXT NOT NULL CHECK (profile IN ('vulnerable','protected')),
  created_at         TEXT NOT NULL
)
"""

TICKET_NOTES_INDEX_SQL = """
CREATE INDEX idx_ticket_notes_ticket_id_id
ON ticket_notes(ticket_id, id DESC)
"""

NOTE_COLUMNS = (
    'id', 'ticket_id', 'requester', 'body', 'source_message_key',
    'run_id', 'profile', 'created_at',
)


def _validate_existing_notes(conn: sqlite3.Connection) -> None:
    from .db import DbError

    columns = tuple(r[1] for r in conn.execute('PRAGMA table_info(ticket_notes)'))
    if columns != NOTE_COLUMNS:
        raise DbError('schema v2 không có cấu trúc ticket_notes mong đợi')
    refs = {(r[3], r[2], r[4]) for r in conn.execute('PRAGMA foreign_key_list(ticket_notes)')}
    if refs != {
        ('ticket_id', 'tickets', 'id'),
        ('source_message_key', 'messages', 'message_key'),
    }:
        raise DbError('schema v2 thiếu khóa ngoại của ticket_notes')
    indexes = conn.execute('PRAGMA index_list(ticket_notes)').fetchall()
    if not any(r[1] == 'idx_ticket_notes_ticket_id_id' for r in indexes):
        raise DbError('schema v2 thiếu index đọc ghi chú theo ticket')
    index = tuple((r[2], r[3]) for r in conn.execute(
        'PRAGMA index_xinfo(idx_ticket_notes_ticket_id_id)') if r[5])
    if index != (('ticket_id', 0), ('id', 1)):
        raise DbError('schema v2 có index ghi chú không đúng thứ tự')


def _validate_v1_base(conn: sqlite3.Connection) -> None:
    from .db import DbError

    required = {'messages', 'tickets', 'actions', 'llm_calls', 'kb_docs', 'kb_fts'}
    found = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= found:
        raise DbError('schema nền thiếu bảng HMA; từ chối cập nhật memory')
    for table, columns in (
        ('tickets', {'id', 'requester'}),
        ('messages', {'message_key'}),
    ):
        actual = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if not columns <= actual:
            raise DbError(f'schema nền thiếu cột cần thiết trong {table}')


def migrate_ticket_memory(conn: sqlite3.Connection) -> int:
    """Cập nhật v1 -> v2 nguyên tử; v2 hợp lệ thì không thay đổi dữ liệu.

    Caller mở connection có foreign_keys=ON và không có transaction đang mở.
    Hàm không tạo backup, không khôi phục snapshot, không khởi động/dừng HMA.
    """
    from .db import DbError, transaction

    if conn.in_transaction:
        raise DbError('migration phải chạy ngoài transaction hiện có')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise DbError('migration yêu cầu foreign_keys=ON')

    with transaction(conn):
        version = int(conn.execute('PRAGMA user_version').fetchone()[0])
        if version not in (1, MEMORY_SCHEMA_VERSION):
            raise DbError(f'không hỗ trợ migration memory từ schema {version}')
        _validate_v1_base(conn)
        if version == 1:
            conn.execute(TICKET_NOTES_TABLE_SQL)
            conn.execute(TICKET_NOTES_INDEX_SQL)
            conn.execute(f'PRAGMA user_version = {MEMORY_SCHEMA_VERSION}')
        _validate_existing_notes(conn)
    return MEMORY_SCHEMA_VERSION
