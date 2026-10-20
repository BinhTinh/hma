"""Truy cập IMAP/SMTP (giai đoạn 1). Chỉ lo giao thức; chưa có logic nghiệp vụ."""
from __future__ import annotations

import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

log = logging.getLogger(__name__)

_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_APPENDUID = re.compile(rb"APPENDUID\s+(\d+)\s+(\d+)")
_DIGITS = re.compile(rb"\d+")


class MailError(RuntimeError):
    """Lỗi kết nối, TLS, xác thực hoặc giao thức mail. Thông báo không chứa mật khẩu."""


@dataclass(frozen=True)
class MailboxInfo:
    name: str
    exists: int
    uidvalidity: int | None
    readonly: bool


def make_ssl_context(ca_file: str | Path) -> ssl.SSLContext:
    """Ngữ cảnh TLS chỉ tin CA của lab, bắt buộc xác minh chứng chỉ và hostname.

    ssl.create_default_context(cafile=...) đặt CERT_REQUIRED và check_hostname=True;
    khi có cafile thì không nạp thêm CA hệ thống.
    """
    p = Path(ca_file)
    if not p.is_file():
        raise MailError(f"không thấy tệp CA: {p}")
    try:
        ctx = ssl.create_default_context(cafile=str(p))
    except (ssl.SSLError, OSError) as exc:
        raise MailError(f"không nạp được CA từ {p}: {exc}") from exc
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise MailError("ngữ cảnh TLS không bắt buộc xác minh chứng chỉ")
    return ctx


def quote_mailbox(name: str) -> str:
    """Đặt tên hộp thư trong dấu nháy kép (imaplib giữ nguyên đối số đã có nháy kép)."""
    if any(c in name for c in "\r\n\x00"):
        raise ValueError("tên hộp thư chứa ký tự điều khiển")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list_line(line: bytes) -> tuple[str, str, tuple[str, ...]] | None:
    """Tách một dòng LIST: trả (tên hộp, ký tự phân cách, cờ) hoặc None nếu không đọc được."""
    m = _LIST_LINE.match(line.strip())
    if not m:
        return None
    name = m.group("name").strip()
    if name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2:
        name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    delim = m.group("delim")
    delim_s = "" if delim == b"NIL" else delim.strip(b'"').decode("ascii", "replace")
    flags = tuple(f.decode("ascii", "replace") for f in m.group("flags").split())
    return name.decode("utf-8", "replace"), delim_s, flags


def header_summary(raw_header: bytes) -> dict:
    """Trích các tiêu đề cần cho chính sách gửi thư (RFC 3834) từ phần header thô."""
    msg = message_from_bytes(raw_header, policy=policy.default)

    def one(name: str) -> str | None:
        v = msg.get(name)
        return None if v is None else str(v).strip()

    from_addr = parseaddr(one("From") or "")[1].lower()
    rp_raw = one("Return-Path")
    return_path = None
    if rp_raw is not None:
        return_path = parseaddr(rp_raw)[1].lower()  # "<>" cho địa chỉ rỗng -> ""
    return {
        "from": from_addr,
        "return_path": return_path,
        "return_path_present": rp_raw is not None,
        "subject": one("Subject"),
        "message_id": one("Message-ID"),
        "date": one("Date"),
        "auto_submitted": one("Auto-Submitted"),
        "precedence": one("Precedence"),
        "has_list_header": any(k.lower().startswith("list-") for k in msg.keys()),
    }


def build_message(*, from_addr: str, to_addrs: list[str], subject: str, body: str,
                  extra_headers: dict[str, str] | None = None, msgid_domain: str | None = None) -> EmailMessage:
    """Dựng thư văn bản thuần UTF-8. Giá trị tiêu đề chứa xuống dòng bị từ chối (chống header injection)."""
    if not to_addrs:
        raise ValueError("thiếu người nhận")
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=msgid_domain or from_addr.rpartition("@")[2] or None)
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return msg


class ImapSession:
    """Một phiên IMAP: kết nối rồi STARTTLS (bắt buộc, có xác minh) rồi đăng nhập.

    Nếu server không có STARTTLS thì imaplib báo lỗi và KHÔNG đăng nhập bằng văn bản thuần.
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 ssl_context: ssl.SSLContext, timeout: float = 30.0):
        self.host, self.port, self.user = host, port, user
        self._password = password
        self._ctx = ssl_context
        self._timeout = timeout
        self._imap: imaplib.IMAP4 | None = None
        self.selected: MailboxInfo | None = None

    def __enter__(self) -> "ImapSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def raw(self) -> imaplib.IMAP4:
        if self._imap is None:
            raise MailError("phiên IMAP chưa mở")
        return self._imap

    def open(self) -> None:
        try:
            self._imap = imaplib.IMAP4(self.host, self.port, timeout=self._timeout)
            self._imap.starttls(ssl_context=self._ctx)
            self._imap.login(self.user, self._password)
        except (imaplib.IMAP4.error, ssl.SSLError, OSError) as exc:
            self.close()
            raise MailError(f"IMAP {self.host}:{self.port}: {type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        imap, self._imap = self._imap, None
        self.selected = None
        if imap is None:
            return
        try:
            imap.logout()
        except Exception:  # đóng best-effort; lỗi lúc đóng không che lỗi gốc
            try:
                imap.shutdown()
            except Exception:
                pass

    def tls_info(self) -> dict:
        sock = getattr(self.raw, "sock", None)
        if not isinstance(sock, ssl.SSLSocket):
            return {"tls": False}
        cert = sock.getpeercert() or {}
        subject = dict(x[0] for x in cert.get("subject", ()))
        san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        issuer = dict(x[0] for x in cert.get("issuer", ()))
        return {"tls": True, "version": sock.version(), "cipher": (sock.cipher() or ("",))[0],
                "subject_cn": subject.get("commonName"), "issuer_cn": issuer.get("commonName"), "san_dns": san}

    def _ok(self, result: tuple, what: str):
        typ, data = result
        if typ != "OK":
            raise MailError(f"IMAP {what} thất bại: {typ} {data}")
        return data

    def capabilities(self) -> frozenset[str]:
        """Hỏi lại CAPABILITY sau khi đăng nhập (tập lệnh có thể khác trước đó)."""
        data = self._ok(self.raw.capability(), "CAPABILITY")
        return frozenset(data[0].decode("ascii", "replace").upper().split())

    def list_mailboxes(self) -> list[tuple[str, str, tuple[str, ...]]]:
        data = self._ok(self.raw.list(), "LIST")
        out = []
        for item in data:
            if isinstance(item, bytes):
                parsed = parse_list_line(item)
                if parsed:
                    out.append(parsed)
        return out

    def select(self, mailbox: str = "INBOX", readonly: bool = True) -> MailboxInfo:
        data = self._ok(self.raw.select(mailbox, readonly=readonly), f"SELECT {mailbox}")
        exists = int(data[0]) if data and data[0] is not None else 0
        _, uv = self.raw.response("UIDVALIDITY")
        uidvalidity = int(uv[0]) if uv and uv[0] else None
        self.selected = MailboxInfo(mailbox, exists, uidvalidity, readonly)
        return self.selected

    def search_uids(self, *criteria: str) -> list[int]:
        data = self._ok(self.raw.uid("SEARCH", None, *(criteria or ("ALL",))), "UID SEARCH")
        return [int(x) for x in _DIGITS.findall(data[0] or b"")]

    def _fetch_literal(self, uid: int, item: str, marker: bytes) -> bytes:
        data = self._ok(self.raw.uid("FETCH", str(uid), f"({item})"), f"UID FETCH {uid}")
        for part in data:
            if isinstance(part, tuple) and marker in part[0].upper():
                return part[1]
        raise MailError(f"UID FETCH {uid}: không có dữ liệu {item} (thư không còn?)")

    def fetch_header(self, uid: int) -> bytes:
        return self._fetch_literal(uid, "BODY.PEEK[HEADER]", b"BODY[HEADER]")

    def fetch_raw(self, uid: int) -> bytes:
        return self._fetch_literal(uid, "BODY.PEEK[]", b"BODY[]")

    def fetch_flags(self, uid: int) -> frozenset[str]:
        data = self._ok(self.raw.uid("FETCH", str(uid), "(FLAGS)"), f"UID FETCH {uid} FLAGS")
        for part in data:
            blob = part[0] if isinstance(part, tuple) else part
            if isinstance(blob, bytes) and b"FLAGS" in blob.upper():
                return frozenset(f.decode("ascii", "replace") for f in imaplib.ParseFlags(blob))
        raise MailError(f"UID FETCH {uid}: không có FLAGS")

    def add_flags(self, uid: int, flags: str) -> None:
        self._ok(self.raw.uid("STORE", str(uid), "+FLAGS.SILENT", f"({flags})"), f"UID STORE {uid}")

    def append(self, mailbox: str, raw_message: bytes, flags: str | None = None) -> int | None:
        """APPEND thư vào hộp; trả UID mới nếu server có UIDPLUS (APPENDUID), ngược lại None."""
        imap = self.raw
        data = self._ok(imap.append(mailbox, flags, None, raw_message), f"APPEND {mailbox}")
        for blob in list(data) + [x for x in (imap.response("APPENDUID")[1] or [])]:
            if isinstance(blob, bytes):
                m = _APPENDUID.search(blob) or re.match(rb"^\s*(\d+)\s+(\d+)\s*$", blob)
                if m:
                    return int(m.group(2))
        return None

    def move(self, uid: int, dest: str, caps: frozenset[str] | None = None) -> str:
        """Chuyển thư sang hộp khác một cách an toàn. Trả tên phương án đã dùng.

        - Có MOVE: UID MOVE (RFC 6851).
        - Không MOVE nhưng có UIDPLUS: UID COPY + UID STORE \\Deleted + UID EXPUNGE <uid>.
        - Thiếu cả hai: từ chối. KHÔNG bao giờ dùng EXPUNGE không kèm UID.
        """
        if self.selected is None or self.selected.readonly:
            raise MailError("move cần hộp được SELECT ở chế độ ghi")
        caps = caps if caps is not None else self.capabilities()
        if "MOVE" not in caps and "UIDPLUS" not in caps:
            raise MailError("server không hỗ trợ MOVE lẫn UIDPLUS; từ chối chuyển thư")
        imap = self.raw
        dest_q = quote_mailbox(dest)
        if "MOVE" in caps:
            self._ok(imap.xatom("UID", "MOVE", str(uid), dest_q), f"UID MOVE {uid}")
            return "UID MOVE"
        self._ok(imap.uid("COPY", str(uid), dest_q), f"UID COPY {uid}")
        self.add_flags(uid, r"\Deleted")
        self._ok(imap.xatom("UID", "EXPUNGE", str(uid)), f"UID EXPUNGE {uid}")
        return "UID COPY + STORE \\Deleted + UID EXPUNGE"


class SmtpSender:
    """Gửi thư qua SMTP submission: STARTTLS bắt buộc rồi đăng nhập."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 ssl_context: ssl.SSLContext, timeout: float = 30.0):
        self.host, self.port, self.user = host, port, user
        self._password = password
        self._ctx = ssl_context
        self._timeout = timeout

    def check(self) -> dict:
        """Kết nối, STARTTLS, đăng nhập rồi thoát; không gửi gì. Trả thông tin TLS."""
        with self._connect() as smtp:
            return self._tls_info(smtp)

    def send(self, msg: EmailMessage, to_addrs: list[str], envelope_from: str | None = None) -> None:
        sender = envelope_from or self.user
        with self._connect() as smtp:
            refused = smtp.send_message(msg, from_addr=sender, to_addrs=to_addrs)
        if refused:
            raise MailError(f"SMTP từ chối một số người nhận: {refused}")

    def _connect(self) -> smtplib.SMTP:
        smtp = None
        try:
            # Truyền host vào constructor: starttls() dùng host này làm server_hostname để kiểm tra chứng chỉ.
            smtp = smtplib.SMTP(self.host, self.port, timeout=self._timeout)
            smtp.starttls(context=self._ctx)  # không có STARTTLS thì ném SMTPNotSupportedError
            smtp.login(self.user, self._password)
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:
                    pass
            raise MailError(f"SMTP {self.host}:{self.port}: {type(exc).__name__}: {exc}") from exc
        return smtp

    @staticmethod
    def _tls_info(smtp: smtplib.SMTP) -> dict:
        sock = smtp.sock
        if not isinstance(sock, ssl.SSLSocket):
            return {"tls": False}
        cert = sock.getpeercert() or {}
        subject = dict(x[0] for x in cert.get("subject", ()))
        return {"tls": True, "version": sock.version(), "subject_cn": subject.get("commonName")}
