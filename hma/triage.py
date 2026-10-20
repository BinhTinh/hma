"""Phân tích thư và quy tắc thuần logic (giai đoạn 2a). Không đụng mạng, không đụng CSDL."""
from __future__ import annotations

import html as html_lib
import re
import unicodedata
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import Message
from email.utils import parseaddr

_TICKET = re.compile(r"\bTKT-\d{4}\b")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_BREAKS = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li)\s*/?>")
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\f\v]+")
_WS = re.compile(r"\s+")
_BULK_PRECEDENCE = {"bulk", "list", "junk"}


@dataclass(frozen=True)
class ParsedMail:
    sender: str
    return_path: str | None
    return_path_present: bool
    subject: str
    message_id: str | None
    references: str | None
    auto_submitted: str | None
    precedence: str | None
    has_list_header: bool
    body_text: str
    truncated: bool


@dataclass(frozen=True)
class ReplyBlock:
    kind: str      # "forbid": không được trả lời (không phải lỗi) | "human": cần người xem
    code: str
    message: str


def _html_to_text(markup: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _BREAKS.sub("\n", text)
    text = _TAGS.sub(" ", text)
    return html_lib.unescape(text)


def _part_text(part: Message) -> str:
    try:
        return str(part.get_content())
    except (LookupError, UnicodeDecodeError, KeyError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def _extract_body(msg: Message) -> str:
    text = ""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        text = _part_text(part)
    if not text.strip():
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            text = _html_to_text(_part_text(part))
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", ln).rstrip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_mail(raw: bytes, body_limit: int) -> ParsedMail:
    """Đọc thư thô thành ParsedMail. Phần thân dài hơn body_limit bị cắt và đặt truncated=True."""
    if body_limit <= 0:
        raise ValueError("body_limit phải > 0")
    msg = message_from_bytes(raw, policy=policy.default)

    def one(name: str) -> str | None:
        v = msg.get(name)
        return None if v is None else str(v).strip()

    rp_raw = one("Return-Path")
    body = _extract_body(msg)
    truncated = len(body) > body_limit
    return ParsedMail(
        sender=parseaddr(one("From") or "")[1].lower(),
        return_path=None if rp_raw is None else parseaddr(rp_raw)[1].lower(),
        return_path_present=rp_raw is not None,
        subject=one("Subject") or "",
        message_id=one("Message-ID"),
        references=one("References"),
        auto_submitted=one("Auto-Submitted"),
        precedence=one("Precedence"),
        has_list_header=any(k.lower().startswith("list-") for k in msg.keys()),
        body_text=body[:body_limit],
        truncated=truncated,
    )


def sender_class(address: str, mapping: dict[str, str]) -> str | None:
    """Loại nguồn theo domain. Chỉ khớp đúng domain: 'x.customer.test' hay 'evilcustomer.test' không khớp."""
    addr = (address or "").strip().lower()
    if addr.count("@") != 1:
        return None
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return None
    return mapping.get(domain)


def extract_ticket_ids(*texts: str | None) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in _TICKET.findall(t or ""):
            if m not in seen:
                seen.append(m)
    return seen


def subject_key(subject: str | None) -> str:
    """Khóa nối chủ đề: NFC, không phân biệt hoa thường, bỏ mọi khoảng trắng.

    Bỏ khoảng trắng vì có thư (M02) bị mất dấu cách giữa hai encoded-word khi giải mã RFC 2047.
    """
    return _WS.sub("", unicodedata.normalize("NFC", subject or "").casefold())


def reply_block(mail: ParsedMail) -> ReplyBlock | None:
    """Có được tự động trả lời thư này không? (RFC 3834). None nghĩa là được."""
    auto = (mail.auto_submitted or "").split(";")[0].strip().lower()
    if auto and auto != "no":
        return ReplyBlock("forbid", "auto_submitted", f"Thư tự động (Auto-Submitted: {mail.auto_submitted}); không trả lời.")
    if mail.has_list_header or (mail.precedence or "").strip().lower() in _BULK_PRECEDENCE:
        return ReplyBlock("forbid", "bulk_mail", "Thư hàng loạt (List-* hoặc Precedence); không trả lời.")
    if not mail.return_path_present:
        return ReplyBlock("human", "missing_return_path", "Thiếu Return-Path; không đoán địa chỉ nhận.")
    if mail.return_path == "":
        return ReplyBlock("forbid", "null_return_path", "Return-Path rỗng (<>); không bao giờ trả lời.")
    if mail.return_path != mail.sender:
        return ReplyBlock("human", "return_path_mismatch", "Return-Path khác From; cần người xem.")
    return None
