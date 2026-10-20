"""Bước nghiệm thu A0: đo trên hạ tầng thật những điều đặc tả còn để mở (giai đoạn 1)."""
from __future__ import annotations

import getpass
import os
import sys
import time
from typing import Callable

from .config import Config
from .mailio import (ImapSession, MailError, SmtpSender, build_message, header_summary,
                     make_ssl_context)

Say = Callable[[str], None]
MAX_ROWS = 60


def get_password(cfg: Config) -> str:
    pw = os.environ.get(cfg.mail.password_env)
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass(f"Mật khẩu của {cfg.mail.user} (không hiển thị): ")
    raise MailError(f"chưa đặt biến môi trường {cfg.mail.password_env} và không có terminal để hỏi")


def _short(s: str | None, n: int) -> str:
    s = "-" if s is None else s
    return s if len(s) <= n else s[: n - 1] + "…"


def run_a0(cfg: Config, password: str, *, send_test: str | None, move_test: bool, say: Say = print) -> int:
    ctx = make_ssl_context(cfg.mail.ca_file)
    failures = 0
    m = cfg.mail

    with ImapSession(m.host, m.imap_port, m.user, password, ctx) as s:
        say(f"== 1. IMAP {m.host}:{m.imap_port}: STARTTLS (xác minh CA + hostname) và đăng nhập: OK")
        ti = s.tls_info()
        say(f"   TLS {ti.get('version')} | cipher {ti.get('cipher')} | CN={ti.get('subject_cn')} "
            f"| issuer={ti.get('issuer_cn')} | SAN={ti.get('san_dns')}")
        caps = s.capabilities()
        say("   CAPABILITY: " + " ".join(sorted(caps)))
        has_move, has_uidplus = "MOVE" in caps, "UIDPLUS" in caps
        path = "UID MOVE" if has_move else ("UID COPY + STORE + UID EXPUNGE" if has_uidplus else "KHÔNG CÓ (route archive bị từ chối)")
        say(f"   MOVE={has_move} UIDPLUS={has_uidplus} => cách chuyển thư vào Archive: {path}")

        say("== 2. Danh sách hộp thư")
        boxes = s.list_mailboxes()
        for name, delim, flags in boxes:
            say(f"   {name!r} delim={delim!r} flags={' '.join(flags)}")
        has_archive = any(n == "Archive" for n, _, _ in boxes)
        say(f"   Có hộp 'Archive': {has_archive}")
        if not has_archive:
            failures += 1

        say("== 3. INBOX (chỉ đọc) và header của từng thư")
        info = s.select("INBOX", readonly=True)
        say(f"   EXISTS={info.exists} UIDVALIDITY={info.uidvalidity}")
        uids = s.search_uids("ALL")
        say(f"   UID: {uids[:MAX_ROWS]}{' ...' if len(uids) > MAX_ROWS else ''}")
        flags_before = {u: s.fetch_flags(u) for u in uids}  # chụp cờ TRƯỚC mọi lệnh đọc nội dung
        rows = []
        for uid in uids[:MAX_ROWS]:
            h = header_summary(s.fetch_header(uid))
            rows.append((uid, h))
        say(f"   {'UID':>4} | {'From':<26} | {'Return-Path':<26} | RP=From | MsgID | Auto | List/Prec")
        for uid, h in rows:
            rp = "(thiếu)" if not h["return_path_present"] else (h["return_path"] or "<>")
            same = h["return_path_present"] and h["return_path"] == h["from"]
            say(f"   {uid:>4} | {_short(h['from'], 26):<26} | {_short(rp, 26):<26} | {str(same):<7} | "
                f"{str(bool(h['message_id'])):<5} | {str(h['auto_submitted']):<4} | "
                f"{h['has_list_header'] or bool(h['precedence'])}")
        n = len(rows)
        if n:
            say(f"   Tổng {n}: có Return-Path {sum(h['return_path_present'] for _, h in rows)}/{n}; "
                f"Return-Path = From {sum(h['return_path_present'] and h['return_path'] == h['from'] for _, h in rows)}/{n}; "
                f"có Message-ID {sum(bool(h['message_id']) for _, h in rows)}/{n}; "
                f"có Auto-Submitted {sum(bool(h['auto_submitted']) for _, h in rows)}/{n}; "
                f"có List-*/Precedence {sum(h['has_list_header'] or bool(h['precedence']) for _, h in rows)}/{n}")

        say("== 4. Kiểm tra BODY.PEEK không đặt cờ \\Seen (mở INBOX ở chế độ ghi)")
        s.select("INBOX", readonly=False)
        target = next((u for u in uids if r"\Seen" not in flags_before[u]), None)
        if target is None:
            say("   Bỏ qua: không có thư nào chưa đọc để thử.")
        else:
            s.fetch_raw(target)
            after = s.fetch_flags(target)
            ok = r"\Seen" not in after
            say(f"   UID {target}: cờ lúc đầu={sorted(flags_before[target])} sau khi đọc={sorted(after)} "
                f"=> BODY.PEEK không đổi \\Seen: {ok}")
            if not ok:
                failures += 1

        if move_test:
            say("== 5. Thử chuyển thư (dùng thư thử do chương trình tự tạo, không đụng thư seed)")
            if not (has_move or has_uidplus):
                say("   BỎ QUA: server thiếu cả MOVE lẫn UIDPLUS.")
                failures += 1
            else:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                msg = build_message(from_addr=m.user, to_addrs=[m.user],
                                    subject=f"[HMA-A0] thư thử chuyển {stamp}", body="Thư thử của A0, có thể xóa.")
                mid = str(msg["Message-ID"])
                s.select("INBOX", readonly=False)
                new_uid = s.append("INBOX", msg.as_bytes())
                if new_uid is None:
                    found = s.search_uids("HEADER", "Message-ID", f'"{mid}"')
                    new_uid = found[0] if found else None
                if new_uid is None:
                    say("   THẤT BẠI: không tìm lại được thư thử sau APPEND.")
                    failures += 1
                else:
                    used = s.move(new_uid, "Archive", caps)
                    still = s.search_uids("HEADER", "Message-ID", f'"{mid}"')
                    s.select("Archive", readonly=False)
                    in_arch = s.search_uids("HEADER", "Message-ID", f'"{mid}"')
                    ok = (not still) and len(in_arch) == 1
                    say(f"   Phương án: {used} | còn trong INBOX: {bool(still)} | trong Archive: {len(in_arch)} => {'ĐẠT' if ok else 'THẤT BẠI'}")
                    if not ok:
                        failures += 1
                    if in_arch and has_uidplus:
                        s.add_flags(in_arch[0], r"\Deleted")
                        s.raw.xatom("UID", "EXPUNGE", str(in_arch[0]))
                        say("   Đã dọn thư thử khỏi Archive (UID EXPUNGE).")
                    elif in_arch:
                        say("   Thư thử còn trong Archive (không có UIDPLUS nên không tự xóa); hãy xóa tay trong Roundcube.")
                s.select("INBOX", readonly=True)

    say(f"== 6. SMTP {m.host}:{m.smtp_port}: STARTTLS (xác minh) và đăng nhập")
    sender = SmtpSender(m.host, m.smtp_port, m.user, password, ctx)
    say(f"   {sender.check()} => OK")
    if send_test:
        to = cfg.handoff[send_test]
        sample = "customer1" if send_test == "client" else "partner1"
        dom = next((d for d, c in cfg.sender_classes.items() if c == send_test), None)
        reply_to = f"{sample}@{dom}" if dom else m.user
        msg = build_message(from_addr=m.user, to_addrs=[to], subject="[HMA-A0] chuyển tiếp thử",
                            body="Thư thử của A0. Hãy bấm Trả lời và xem địa chỉ nhận có là người gửi gốc không.",
                            extra_headers={"Reply-To": reply_to})
        sender.send(msg, [to])
        say(f"   Đã gửi thư thử tới {to} (Reply-To: {reply_to}). Hãy mở hộp thư đó trong Roundcube để xác nhận đã nhận.")
    else:
        say("   (không gửi thư thử; thêm --send-test client hoặc partner nếu muốn)")

    say(f"== KẾT QUẢ A0: {'ĐẠT' if failures == 0 else f'{failures} mục cần xem lại'}")
    return 0 if failures == 0 else 1
