from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .db import transaction

_HEADING = re.compile(r"^#\s*(KB-\d{3})\s*:\s*(.+?)\s*$")
_MAILTO = re.compile(r"\[([^\]]+)\]\(mailto:[^)]+\)")
_WORD = re.compile(r"\w+", re.UNICODE)


class KBError(ValueError):
    """Tệp KB sai định dạng hoặc thư mục KB không hợp lệ."""


@dataclass(frozen=True)
class KBDoc:
    doc_id: str
    title: str
    body: str


def normalize(text: str) -> str:
    """NFC, thống nhất xuống dòng, gỡ cú pháp link mailto dạng [a](mailto:a)."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return _MAILTO.sub(r"\1", text)


def parse_kb_file(path: Path) -> KBDoc:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise KBError(f"{path.name}: không đọc được ({exc})") from exc
    lines = normalize(raw).split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        raise KBError(f"{path.name}: tệp rỗng")
    m = _HEADING.match(lines[first].strip())
    if not m:
        raise KBError(f"{path.name}: dòng đầu phải dạng '# KB-001: Tiêu đề', nhận {lines[first]!r}")
    body = "\n".join(lines[first + 1:]).strip()
    if not body:
        raise KBError(f"{path.name}: thiếu nội dung")
    return KBDoc(doc_id=m.group(1), title=m.group(2), body=body)


def load_kb_dir(kb_dir: Path) -> list[KBDoc]:
    if not kb_dir.is_dir():
        raise KBError(f"thư mục KB không tồn tại: {kb_dir}")
    files = sorted(f for f in kb_dir.iterdir() if f.is_file() and f.name.startswith("KB-"))
    if not files:
        raise KBError(f"không có tệp KB-* trong {kb_dir}")
    docs: list[KBDoc] = []
    seen: dict[str, str] = {}
    for f in files:
        doc = parse_kb_file(f)
        if doc.doc_id in seen:
            raise KBError(f"trùng mã {doc.doc_id}: {seen[doc.doc_id]} và {f.name}")
        seen[doc.doc_id] = f.name
        docs.append(doc)
    return docs


def replace_kb(conn: sqlite3.Connection, docs: list[KBDoc], always_load: tuple[str, ...]) -> None:
    """Xóa toàn bộ KB cũ và nạp lại trong một giao dịch."""
    ids = {d.doc_id for d in docs}
    unknown = sorted(set(always_load) - ids)
    if unknown:
        raise KBError(f"'kb.always_load' tham chiếu mã không có trong KB: {unknown}")
    with transaction(conn):
        conn.execute("DELETE FROM kb_docs")
        conn.execute("DELETE FROM kb_fts")
        for d in docs:
            conn.execute(
                "INSERT INTO kb_docs(doc_id, title, body, always_load) VALUES (?,?,?,?)",
                (d.doc_id, d.title, d.body, 1 if d.doc_id in always_load else 0),
            )
            conn.execute(
                "INSERT INTO kb_fts(doc_id, title, body) VALUES (?,?,?)",
                (d.doc_id, d.title, d.body),
            )


def build_match(query: str) -> str | None:
    """Biến câu hỏi tự do thành truy vấn FTS5 an toàn: "t1" OR "t2" ...

    Chỉ giữ ký tự chữ/số/_; mỗi từ được đặt trong dấu nháy kép nên toán tử FTS5
    (AND, NOT, *, ngoặc...) trong đầu vào không bao giờ được diễn giải.
    """
    toks = _WORD.findall(unicodedata.normalize("NFC", query).lower())
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


def search(conn: sqlite3.Connection, query: str, top_k: int) -> list[dict]:
    if top_k <= 0:
        raise ValueError("top_k phải > 0")
    match = build_match(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT doc_id, title, body FROM kb_fts WHERE kb_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, top_k),
    ).fetchall()
    return [{"doc_id": r["doc_id"], "title": r["title"], "body": r["body"]} for r in rows]


def always_load_docs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_id, title, body FROM kb_docs WHERE always_load = 1 ORDER BY doc_id"
    ).fetchall()
    return [dict(r) for r in rows]
