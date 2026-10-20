"""Truy cập CSDL ở mức bản ghi (giai đoạn 2b). Không tự mở giao dịch: người gọi quyết định ranh giới."""
from __future__ import annotations

import sqlite3

from .db import make_message_key, now_iso


def register_message(conn: sqlite3.Connection, *, mailbox: str, uidvalidity: int, uid: int, sender: str,
                     sender_class: str | None, subject: str, truncated: bool) -> str:
    """Tạo bản ghi thư (trạng thái NEW) nếu chưa có; trả về message_key."""
    key = make_message_key(mailbox, uidvalidity, uid)
    now = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO messages(message_key, mailbox, uidvalidity, uid, sender, sender_class, subject,"
        " status, truncated, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'NEW',?,?,?)",
        (key, mailbox, uidvalidity, uid, sender, sender_class, subject, 1 if truncated else 0, now, now),
    )
    return key


def set_task_type(conn: sqlite3.Connection, key: str, task_type: int) -> None:
    conn.execute("UPDATE messages SET task_type = ?, updated_at = ? WHERE message_key = ?",
                 (task_type, now_iso(), key))


def set_handoff_status(conn: sqlite3.Connection, key: str, status: str) -> None:
    conn.execute("UPDATE messages SET handoff_status = ?, updated_at = ? WHERE message_key = ?",
                 (status, now_iso(), key))


def fetch_ticket(conn: sqlite3.Connection, ticket_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, subject, requester, status, priority, assignee, source_message_key, created_at, updated_at"
        " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def count_replies_today(conn: sqlite3.Connection, recipient: str) -> int:
    """Số thư đã (định) gửi cho người nhận kể từ 00:00 hôm nay, tính cả INTENT và FAILED (an toàn hơn)."""
    today = now_iso()[:10]
    row = conn.execute(
        "SELECT COUNT(*) FROM actions WHERE action_type = 'reply' AND target = ? AND created_at >= ?",
        (recipient, today)).fetchone()
    return int(row[0])


def record_action(conn: sqlite3.Connection, key: str, action_type: str, target: str | None,
                  status: str, detail: str | None = None) -> int:
    """Ghi một hành động. Vi phạm UNIQUE(message_key, action_type) ném sqlite3.IntegrityError."""
    cur = conn.execute(
        "INSERT INTO actions(message_key, action_type, target, status, detail, created_at) VALUES (?,?,?,?,?,?)",
        (key, action_type, target, status, detail, now_iso()))
    return int(cur.lastrowid)


def set_action_status(conn: sqlite3.Connection, action_id: int, status: str, detail: str | None = None) -> None:
    conn.execute("UPDATE actions SET status = ?, detail = COALESCE(?, detail) WHERE id = ?",
                 (status, detail, action_id))


def is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    """Phân biệt trùng UNIQUE (hành động đã làm) với lỗi khác như khóa ngoại (lỗi lập trình)."""
    return "UNIQUE constraint failed" in str(exc)

# --- Memory ticket (2C) ---

NOTE_BODY_MAX_CHARS = 4000
NOTE_READ_DEFAULT_LIMIT = 10
NOTE_READ_MAX_LIMIT = 50

_NOTE_COLUMNS = (
    "id, ticket_id, requester, body, source_message_key, run_id, profile, created_at"
)


def _require_note_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} phải là chuỗi không rỗng và không chứa NUL")


def append_ticket_note(conn: sqlite3.Connection, *, ticket_id: str, body: str,
                       source_message_key: str, run_id: str, profile: str) -> dict:
    """Lưu ghi chú và trả bản ghi nội bộ. Caller phải mở transaction trước.

    Không nhận requester từ caller, không tự commit và không kiểm ownership.
    source_message_key/run_id/profile phải do lớp thực thi cung cấp, không lấy
    từ arguments do model tự khai. Chưa có idempotency ở cấp tool.
    """
    if not conn.in_transaction:
        raise RuntimeError("append_ticket_note phải được gọi trong transaction()")
    for name, value in (
        ("ticket_id", ticket_id), ("body", body),
        ("source_message_key", source_message_key), ("run_id", run_id), ("profile", profile),
    ):
        _require_note_text(name, value)
    if len(body) > NOTE_BODY_MAX_CHARS:
        raise ValueError(f"body không được quá {NOTE_BODY_MAX_CHARS} ký tự")
    if profile not in ("vulnerable", "protected"):
        raise ValueError("profile phải là vulnerable hoặc protected")

    ticket = conn.execute("SELECT requester FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        raise ValueError(f"ticket không tồn tại: {ticket_id}")
    requester = ticket["requester"]
    _require_note_text("requester trong ticket", requester)

    cur = conn.execute(
        "INSERT INTO ticket_notes(ticket_id, requester, body, source_message_key, run_id, profile, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (ticket_id, requester, body, source_message_key, run_id, profile, now_iso()),
    )
    row = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM ticket_notes WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def list_ticket_notes(conn: sqlite3.Connection, ticket_id: str, *,
                      limit: int = NOTE_READ_DEFAULT_LIMIT) -> list[dict]:
    """Đọc bản ghi nội bộ, mới nhất trước; không tự mở transaction hoặc kiểm quyền.

    Ticket không có ghi chú hoặc không tồn tại đều cho danh sách rỗng.
    Metadata trả về không được tự động đưa nguyên vẹn vào tool result cho model.
    """
    _require_note_text("ticket_id", ticket_id)
    if type(limit) is not int or not 1 <= limit <= NOTE_READ_MAX_LIMIT:
        raise ValueError(f"limit phải là số nguyên từ 1 đến {NOTE_READ_MAX_LIMIT}")
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM ticket_notes WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
        (ticket_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
