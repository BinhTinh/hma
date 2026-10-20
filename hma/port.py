"""Cổng thư thật cho Toolbox (giai đoạn 4): gửi qua SMTP, chuyển thư qua IMAP; cùng bộ ghi lại để lập báo cáo."""
from __future__ import annotations

from .mailio import SmtpSender, build_message


class CapturingPort:
    """Ghi lại những gì ĐÃ gửi thành công (sau khi cổng thật không ném lỗi). delegate=None nghĩa là chạy thử, không gửi gì."""

    def __init__(self, delegate=None) -> None:
        self.delegate = delegate
        self.replies: list[dict] = []
        self.handoffs: list[dict] = []
        self.archived: list[int] = []

    def send_reply(self, **kw) -> None:
        if self.delegate is not None:
            self.delegate.send_reply(**kw)
        self.replies.append(kw)

    def send_handoff(self, **kw) -> None:
        if self.delegate is not None:
            self.delegate.send_handoff(**kw)
        self.handoffs.append(kw)

    def archive(self, uid: int) -> None:
        if self.delegate is not None:
            self.delegate.archive(uid)
        self.archived.append(uid)

    def drain(self) -> dict:
        out = {"replies": self.replies, "handoffs": self.handoffs, "archived": self.archived}
        self.replies, self.handoffs, self.archived = [], [], []
        return out


class ImapSmtpPort:
    """Gửi thư bằng SmtpSender và chuyển thư bằng ImapSession đã SELECT INBOX ở chế độ ghi."""

    def __init__(self, smtp: SmtpSender, imap, support_address: str, caps: frozenset[str], archive_box: str = "Archive"):
        self.smtp, self.imap, self.support = smtp, imap, support_address
        self.caps, self.archive_box = caps, archive_box

    def send_reply(self, *, to: str, subject: str, body: str, in_reply_to: str | None, references: str | None) -> None:
        headers = {"Auto-Submitted": "auto-replied"}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
        if references:
            headers["References"] = references
        msg = build_message(from_addr=self.support, to_addrs=[to], subject=subject, body=body, extra_headers=headers)
        self.smtp.send(msg, [to])

    def send_handoff(self, *, to: str, subject: str, body: str, reply_to: str) -> None:
        msg = build_message(from_addr=self.support, to_addrs=[to], subject=subject, body=body,
                            extra_headers={"Reply-To": reply_to})
        self.smtp.send(msg, [to])

    def archive(self, uid: int) -> None:
        self.imap.move(uid, self.archive_box, self.caps)
