"""Runner: xử lý từng thư trong INBOX, quản lý trạng thái, leo thang cho người, lập báo cáo (giai đoạn 4, mở rộng 7a).

Chạy thử (mặc định, KHÔNG gửi thư, KHÔNG đổi hộp thư, KHÔNG đụng CSDL thật):
    python -m hma.runner [--limit N] [--uid U ...]
Chạy thật một lượt (gửi thư, chuyển thư, ghi CSDL thật):
    python -m hma.runner --live [--yes]
Chạy như dịch vụ (không tương tác, êm khi đang có lượt khác): python -m hma.runner --live --yes --if-idle
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import logging
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import db, health, kb, prompts, store
from .db import make_message_key, now_iso, transaction
from .runtime_lock import RunnerError, RunLock
from .loop import run_episode
from .port import CapturingPort
from .tools import TOOL_SCHEMAS, MessageContext, ToolSettings, Toolbox
from .memory_context import MemoryExecutionContext
from .triage import ParsedMail, parse_mail, sender_class
from .usage import record_llm_call

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
TERMINAL = frozenset({"DONE", "NEEDS_HUMAN", "SKIPPED"})
RETRY_WAIT_S = (120.0, 600.0)           # chờ sau lần thất bại thứ 1 và thứ 2 trước khi thử lại (chạy thật)
DEFAULT_DAILY_TOKEN_BUDGET = 3_000_000   # con số tạm do tôi đặt; chỉnh bằng --daily-token-budget
DEFAULT_MAX_RUN_SECONDS = 900




@dataclass(frozen=True)
class Limits:
    max_steps: int
    episode_timeout_s: float
    episode_token_cap: int
    body_limit_chars: int
    max_run_messages: int
    daily_token_budget: int = 0          # 0 = không giới hạn

    @classmethod
    def from_config(cls, cfg) -> "Limits":
        a = cfg.agent
        return cls(a.max_steps, a.episode_timeout_s, a.episode_token_cap, a.body_limit_chars, a.max_run_messages)


class MailSource(Protocol):
    uidvalidity: int

    def list_uids(self) -> list[int]: ...
    def fetch_raw(self, uid: int) -> bytes: ...


@dataclass
class Env:
    conn: object
    port: CapturingPort
    llm: object
    settings: ToolSettings
    system_prompt: str
    limits: Limits
    mailbox: str = "INBOX"
    uidvalidity: int = 0
    retry_wait: tuple = (0.0, 0.0)
    now: Callable[[], datetime] = datetime.now
    clock: Callable[[], float] = time.monotonic
    budget_exhausted: bool = False
    memory: MemoryExecutionContext | None = None




def acquire_lock(stack: contextlib.ExitStack, path: Path, if_idle: bool) -> bool:
    """Lấy khóa chạy. Với if_idle, nếu đang có lượt khác thì trả False (êm) thay vì báo lỗi."""
    try:
        stack.enter_context(RunLock(path))
    except RunnerError:
        if if_idle:
            return False
        raise
    return True


def _one_line(text: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _set_status(conn, key: str, status: str, reason: str | None = None) -> None:
    conn.execute("UPDATE messages SET status = ?, reason = ?, updated_at = ? WHERE message_key = ?",
                 (status, reason, now_iso(), key))


def _claim(conn, key: str) -> None:
    now = now_iso()
    conn.execute("UPDATE messages SET status = 'CLAIMED', attempts = attempts + 1, claimed_at = ?, updated_at = ?"
                 " WHERE message_key = ?", (now, now, key))


def _has_nonmemory_effects(conn, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM actions WHERE message_key = ? AND status IN ('INTENT','SENT','DONE') LIMIT 1",
                       (key,)).fetchone()
    return row is not None


def _has_effects(conn, key: str) -> bool:
    if _has_nonmemory_effects(conn, key):
        return True
    return conn.execute(
        "SELECT 1 FROM ticket_notes WHERE source_message_key = ? LIMIT 1", (key,)
    ).fetchone() is not None


def tokens_today(conn, today: str) -> int:
    """Tổng token LLM đã dùng kể từ 00:00 của ngày `today` (YYYY-MM-DD, giờ cục bộ)."""
    row = conn.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM llm_calls WHERE created_at >= ?", (today,)).fetchone()
    return int(row[0])


def check_uidvalidity(conn, mailbox: str, uidvalidity: int) -> None:
    row = conn.execute("SELECT uidvalidity FROM messages WHERE mailbox = ? AND uidvalidity != ? LIMIT 1",
                       (mailbox, uidvalidity)).fetchone()
    if row is not None:
        raise RunnerError(f"UIDVALIDITY của {mailbox} đã đổi ({row[0]} -> {uidvalidity}); UID cũ không còn đáng tin. "
                          "Dừng để người kiểm tra.")


def escalate_to_human(env: Env, key: str, mail: ParsedMail, cls: str, reason: str) -> str:
    """Chuyển thư cho quản trị của loại nguồn khi agent không tự xử lý được (HO-5). Trả SENT|FAILED|EXISTING|NO_TARGET."""
    address = env.settings.handoff.get(cls)
    if not address:
        return "NO_TARGET"
    conn = env.conn
    try:
        with transaction(conn):
            aid = store.record_action(conn, key, "handoff", address, "INTENT", f"escalation:{reason}"[:300])
    except sqlite3.IntegrityError as exc:
        if not store.is_unique_violation(exc):
            raise
        return "EXISTING"
    lines = [f"Lý do cần người xử lý: {reason}", f"Loại nguồn: {cls}", f"Người gửi: {mail.sender}"]
    for t in conn.execute("SELECT id, status, priority FROM tickets WHERE source_message_key = ?", (key,)):
        lines.append(f"Ticket: {t['id']} | trạng thái: {t['status']} | ưu tiên: {t['priority']}")
    lines += ["", "--- Nội dung thư gốc ---", mail.body_text]
    subject = f"[CẦN NGƯỜI XỬ LÝ] {_one_line(mail.subject) or '(không có chủ đề)'}"
    try:
        env.port.send_handoff(to=address, subject=subject, body="\n".join(lines), reply_to=mail.sender)
    except Exception as exc:  # không để lỗi gửi làm hỏng cả lần chạy; ghi lại để `status` hiển thị
        store.set_action_status(conn, aid, "FAILED", f"{type(exc).__name__}: {exc}"[:300])
        store.set_handoff_status(conn, key, "FAILED")
        log.error("không chuyển được thư %s cho người xử lý: %s", key, exc)
        return "FAILED"
    store.set_action_status(conn, aid, "SENT")
    store.set_handoff_status(conn, key, "SENT")
    return "SENT"


def _entry_details(conn, key: str, entry: dict, captured: dict) -> None:
    row = conn.execute("SELECT task_type, handoff_status, attempts FROM messages WHERE message_key = ?", (key,)).fetchone()
    entry["task_type"], entry["handoff_status"], entry["attempts"] = row["task_type"], row["handoff_status"], row["attempts"]
    entry["tickets"] = [dict(r) for r in conn.execute(
        "SELECT id, priority, status FROM tickets WHERE source_message_key = ?", (key,))]
    entry["actions"] = [dict(r) for r in conn.execute(
        "SELECT action_type, target, status, detail FROM actions WHERE message_key = ? ORDER BY id", (key,))]
    entry["replies"] = [{"to": r["to"], "subject": r["subject"], "body": r["body"]} for r in captured["replies"]]
    entry["handoffs"] = [{"to": h["to"], "subject": h["subject"]} for h in captured["handoffs"]]
    entry["archived"] = bool(captured["archived"])
    entry.setdefault("ticket_notes_read", [])
    entry["ticket_notes_written"] = [dict(r) for r in conn.execute(
        "SELECT id, ticket_id, source_message_key, run_id, profile, created_at"
        " FROM ticket_notes WHERE source_message_key = ? ORDER BY id", (key,))]


def process_uid(env: Env, uid: int, raw: bytes, prior) -> dict:
    """Xử lý một thư. `prior` là hàng messages trước đó (hoặc None). Trả về một mục báo cáo."""
    conn = env.conn
    mail = parse_mail(raw, env.limits.body_limit_chars)
    cls = sender_class(mail.sender, env.settings.sender_classes)
    key = store.register_message(conn, mailbox=env.mailbox, uidvalidity=env.uidvalidity, uid=uid, sender=mail.sender,
                                 sender_class=cls, subject=mail.subject, truncated=mail.truncated)
    entry: dict = {"uid": uid, "key": key, "sender": mail.sender, "subject": mail.subject, "sender_class": cls,
                   "steps": 0, "tokens": {}}
    env.port.drain()

    def finish(status: str, reason: str | None, forward_reason: str | None = None) -> dict:
        _set_status(conn, key, status, reason)
        entry["status"], entry["reason"] = status, reason or ""
        if forward_reason is not None and cls is not None:
            entry["escalation"] = escalate_to_human(env, key, mail, cls, forward_reason)
        _entry_details(conn, key, entry, env.port.drain())
        return entry

    if cls is None:
        return finish("SKIPPED", "sender_not_allowed")
    if prior is not None and prior["status"] == "CLAIMED":
        return finish("NEEDS_HUMAN", "interrupted", "interrupted")
    if prior is not None and prior["status"] == "FAILED" and prior["attempts"] >= MAX_ATTEMPTS:
        return finish("NEEDS_HUMAN", "retry_exhausted", "retry_exhausted")

    _claim(conn, key)
    try:
        toolbox = Toolbox(MessageContext(conn, key, uid, mail, env.settings, env.port, memory=env.memory))
        entry["ticket_notes_read"] = toolbox.memory_reads
        res = run_episode(env.llm, toolbox, toolbox.schemas_for_episode(), env.system_prompt, prompts.build_task_message(uid),
                          max_steps=env.limits.max_steps, timeout_s=env.limits.episode_timeout_s,
                          token_cap=env.limits.episode_token_cap,
                          on_call=lambda s, t: record_llm_call(conn, key, s, t))
    except Exception as exc:  # lỗi không lường trước trong lúc xử lý một thư: không dừng cả lần chạy
        log.exception("lỗi khi xử lý %s", key)
        reason = f"exception: {type(exc).__name__}: {exc}"[:300]
        if _has_effects(conn, key):
            return finish("NEEDS_HUMAN", f"partial_effects: {reason}", "partial_effects")
        return finish("FAILED", reason)

    u = res.usage
    entry["steps"] = res.steps
    entry["tokens"] = {"prompt": u.prompt_tokens, "completion": u.completion_tokens, "reasoning": u.reasoning_tokens,
                       "cache_hit": u.cache_hit_tokens, "total": u.total_tokens}
    outcome, reason = res.outcome, (res.reason or toolbox.escalation or "")
    if outcome == "FAILED" and _has_effects(conn, key):
        outcome, reason = "NEEDS_HUMAN", f"partial_effects: {reason}"
    if outcome == "DONE":
        return finish("DONE", reason or None)
    if outcome == "NEEDS_HUMAN":
        return finish("NEEDS_HUMAN", reason or res.status, reason or res.status)
    return finish("FAILED", reason)


def _backoff_active(env: Env, prior) -> bool:
    """Thư FAILED còn trong thời gian chờ trước lần thử lại? (Hết lượt thử thì không chờ: sẽ leo thang ngay.)"""
    if prior is None or prior["status"] != "FAILED" or prior["attempts"] >= MAX_ATTEMPTS or not env.retry_wait:
        return False
    wait = env.retry_wait[max(0, min(prior["attempts"], len(env.retry_wait)) - 1)]
    if wait <= 0:
        return False
    try:
        last = datetime.fromisoformat(prior["updated_at"])
    except (TypeError, ValueError):
        return False
    return (env.now() - last).total_seconds() < wait


def run_batch(env: Env, source: MailSource, *, limit: int | None = None, only: set[int] | None = None,
              say: Callable[[str], None] = print, deadline: float | None = None,
              heartbeat: Callable[[list], None] | None = None) -> list[dict]:
    check_uidvalidity(env.conn, env.mailbox, env.uidvalidity)
    cap = min(limit or env.limits.max_run_messages, env.limits.max_run_messages)
    entries: list[dict] = []
    if heartbeat:
        heartbeat(entries)
    for uid in sorted(source.list_uids()):
        if len(entries) >= cap:
            say(f"Đã đạt giới hạn {cap} thư cho lần chạy này.")
            break
        if only and uid not in only:
            continue
        key = make_message_key(env.mailbox, env.uidvalidity, uid)
        prior = env.conn.execute("SELECT status, attempts, updated_at FROM messages WHERE message_key = ?", (key,)).fetchone()
        if prior is not None and prior["status"] in TERMINAL:
            continue
        if _backoff_active(env, prior):
            continue
        if deadline is not None and env.clock() >= deadline:
            say("Hết thời gian cho lượt chạy này; các thư còn lại để lượt sau.")
            break
        budget = env.limits.daily_token_budget
        if budget and tokens_today(env.conn, env.now().date().isoformat()) >= budget:
            env.budget_exhausted = True
            say(f"Đã dùng hết ngân sách {budget} token của hôm nay; các thư còn lại để ngày mai.")
            break
        raw = source.fetch_raw(uid)  # MailError ở đây là lỗi hạ tầng: để nó dừng cả lần chạy
        entry = process_uid(env, uid, raw, prior)
        entries.append(entry)
        say(f"[{len(entries)}] UID {uid} {entry['sender']} | {_one_line(entry['subject'], 45)} -> {entry['status']}"
            f"{' (' + entry['reason'] + ')' if entry['reason'] else ''}")
        if heartbeat:
            heartbeat(entries)
    return entries


def summarize(entries: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    totals = {k: sum(e["tokens"].get(k, 0) for e in entries) for k in ("prompt", "completion", "reasoning", "cache_hit", "total")}
    return {"messages": len(entries), "by_status": counts, "tokens": totals}


def write_report(path: Path, mode: str, entries: list[dict], started: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"mode": mode, "started": started, "finished": now_iso(), "summary": summarize(entries), "messages": entries}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def finish_pass(entries: list[dict], *, mode: str, started: str, report_dir: Path, stamp: str,
                heartbeat_path: Path | None, budget_exhausted: bool = False) -> Path | None:
    """Kết thúc một lượt: chỉ ghi báo cáo khi có thư được xử lý; luôn cập nhật dấu còn sống (nếu có)."""
    report = write_report(report_dir / f"run-{mode}-{stamp}.json", mode, entries, started) if entries else None
    if heartbeat_path is not None:
        health.write_heartbeat(heartbeat_path, state="idle", mode=mode, processed=len(entries),
                               by_status=summarize(entries)["by_status"], budget_exhausted=budget_exhausted)
    return report


class ImapSource:
    def __init__(self, session, info) -> None:
        if info.uidvalidity is None:
            raise RunnerError("server không trả UIDVALIDITY cho INBOX")
        self.s, self.uidvalidity = session, info.uidvalidity

    def list_uids(self) -> list[int]:
        return self.s.search_uids("ALL")

    def fetch_raw(self, uid: int) -> bytes:
        return self.s.fetch_raw(uid)


def main(argv: list[str] | None = None) -> int:
    from .a0 import get_password
    from .config import load_config
    from .llm import LLMClient
    from .mailio import ImapSession, SmtpSender, make_ssl_context
    from .port import ImapSmtpPort

    ap = argparse.ArgumentParser(prog="python -m hma.runner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/agent.yaml")
    ap.add_argument("--live", action="store_true", help="GỬI THƯ THẬT và ghi CSDL thật (mặc định là chạy thử)")
    ap.add_argument("--yes", action="store_true", help="bỏ qua bước xác nhận khi --live (dùng cho dịch vụ)")
    ap.add_argument("--if-idle", action="store_true", help="nếu đang có lượt chạy khác thì thoát êm, mã 0")
    ap.add_argument("--limit", type=int, default=0, help="số thư tối đa cho lần chạy này")
    ap.add_argument("--uid", type=int, action="append", help="chỉ xử lý UID này (lặp lại được)")
    ap.add_argument("--max-run-seconds", type=int, default=DEFAULT_MAX_RUN_SECONDS,
                    help="ngừng nhận thư mới sau số giây này (0 = không giới hạn)")
    ap.add_argument("--daily-token-budget", type=int, default=DEFAULT_DAILY_TOKEN_BUDGET,
                    help="ngân sách token mỗi ngày khi --live (0 = không giới hạn)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    limits = Limits.from_config(cfg)
    if args.live:
        limits = dataclasses.replace(limits, daily_token_budget=max(0, args.daily_token_budget))
    mode = "live" if args.live else "dry-run"
    if args.live and not args.yes:
        if not sys.stdin.isatty():
            raise RunnerError("--live cần xác nhận tương tác hoặc thêm --yes")
        print("CHẾ ĐỘ THẬT: agent sẽ gửi thư, chuyển thư vào Archive và ghi CSDL thật.")
        if input("Gõ YES để tiếp tục: ").strip() != "YES":
            print("Đã hủy.")
            return 1
    started = now_iso()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    hb_path = cfg.paths.db.parent / "heartbeat.json" if args.live else None
    with contextlib.ExitStack() as stack:
        if args.live and not acquire_lock(stack, cfg.paths.db.parent / "run.lock", args.if_idle):
            print("Đang có lượt chạy khác; bỏ qua lượt này.")
            return 0
        password = get_password(cfg)
        ctx = make_ssl_context(cfg.mail.ca_file)
        llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, thinking=cfg.llm.thinking,
                        timeout_s=cfg.llm.request_timeout_s)
        if args.live:
            conn = db.connect_existing(cfg.paths.db)
            db.check_schema(conn)
        else:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            dry_path = Path(tmp) / "dryrun.db"
            db.init_db(dry_path)
            conn = db.connect(dry_path)
            kb.replace_kb(conn, kb.load_kb_dir(cfg.paths.kb_dir), cfg.kb_always_load)
        stack.callback(conn.close)
        s = stack.enter_context(ImapSession(cfg.mail.host, cfg.mail.imap_port, cfg.mail.user, password, ctx))
        info = s.select("INBOX", readonly=not args.live)
        source = ImapSource(s, info)
        if args.live:
            smtp = SmtpSender(cfg.mail.host, cfg.mail.smtp_port, cfg.mail.user, password, ctx)
            port = CapturingPort(ImapSmtpPort(smtp, s, cfg.mail.user, s.capabilities()))
        else:
            port = CapturingPort(None)
        env = Env(conn, port, llm, ToolSettings.from_config(cfg), prompts.build_system_prompt(kb.always_load_docs(conn)),
                  limits, "INBOX", info.uidvalidity, retry_wait=RETRY_WAIT_S if args.live else (0.0, 0.0))
        deadline = time.monotonic() + args.max_run_seconds if args.max_run_seconds > 0 else None
        beat = ((lambda es: health.write_heartbeat(hb_path, state="running", mode=mode, processed=len(es)))
                if hb_path else None)
        print(f"Chế độ: {mode} | INBOX: {info.exists} thư, UIDVALIDITY {info.uidvalidity}")
        entries = run_batch(env, source, limit=args.limit or None, only=set(args.uid) if args.uid else None,
                            deadline=deadline, heartbeat=beat)
    report = finish_pass(entries, mode=mode, started=started, report_dir=cfg.paths.log_dir, stamp=stamp,
                         heartbeat_path=hb_path, budget_exhausted=env.budget_exhausted)
    if not entries:
        print("Không có thư mới cần xử lý.")
        return 0
    summ = summarize(entries)
    print(f"\nTổng: {summ['messages']} thư | {summ['by_status']} | token: {summ['tokens']}")
    print(f"Báo cáo: {report}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # in lỗi gọn cho người dùng; traceback đầy đủ khi cần: python -X dev
        from .llm import LLMError
        from .mailio import MailError
        if isinstance(exc, (RunnerError, MailError, LLMError, ValueError, db.DbError)):
            print(f"LỖI: {exc}", file=sys.stderr)
            sys.exit(2)
        raise
