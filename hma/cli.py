"""Dòng lệnh: init-db, migrate-db, kb-search, status và a0."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db, kb
from .config import Config, ConfigError, load_config
from .mailio import MailError

log = logging.getLogger("hma")
DEFAULT_CONFIG = "config/agent.yaml"


def _cmd_init_db(cfg: Config, args: argparse.Namespace) -> int:
    docs = kb.load_kb_dir(cfg.paths.kb_dir)
    db.init_db(cfg.paths.db, force=args.force)
    conn = db.connect(cfg.paths.db)
    try:
        kb.replace_kb(conn, docs, cfg.kb_always_load)
    finally:
        conn.close()
    print(f"Đã khởi tạo CSDL: {cfg.paths.db}")
    print(f"  ticket seed: 3 | tài liệu KB: {len(docs)} | KB nạp cố định: {', '.join(cfg.kb_always_load)}")
    return 0


def _cmd_migrate_db(cfg: Config, args: argparse.Namespace) -> int:
    from .memory_schema import migrate_ticket_memory

    conn = db.connect_existing(cfg.paths.db)
    try:
        before = int(conn.execute("PRAGMA user_version").fetchone()[0])
        after = migrate_ticket_memory(conn)
        db.check_schema(conn)
    finally:
        conn.close()
    if before == after:
        print(f"CSDL đã ở lược đồ v{after}; không thay đổi dữ liệu: {cfg.paths.db}")
    else:
        print(f"Đã cập nhật CSDL v{before} -> v{after}: {cfg.paths.db}")
    return 0


def _cmd_kb_search(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect_existing(cfg.paths.db)
    try:
        db.check_schema(conn)
        hits = kb.search(conn, args.query, args.top_k or cfg.agent.kb_top_k)
    finally:
        conn.close()
    if not hits:
        print("(không có kết quả)")
    for h in hits:
        print(f"[{h['doc_id']}] {h['title']}\n    {h['body']}")
    return 0


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    conn = db.connect_existing(cfg.paths.db)
    try:
        db.check_schema(conn)
        print("messages theo trạng thái:")
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM messages GROUP BY status").fetchall()
        for r in rows or []:
            print(f"  {r['status']}: {r['n']}")
        if not rows:
            print("  (chưa có)")
        for table in ("tickets", "ticket_notes", "actions", "llm_calls", "kb_docs"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {n}")
    finally:
        conn.close()
    return 0


def _cmd_a0(cfg: Config, args: argparse.Namespace) -> int:
    from .a0 import get_password, run_a0

    return run_a0(cfg, get_password(cfg), send_test=args.send_test, move_test=args.move_test)


def _cmd_profile(cfg: Config, args: argparse.Namespace) -> int:
    from .profiles import ProfileError, ProfileNotSelected, ProfileSelector
    from .runtime_lock import RunBusy

    try:
        selector = ProfileSelector(cfg.paths.db)
        if args.profile_command == 'show':
            try:
                name = selector.read()
            except ProfileNotSelected:
                name = None
            result = {'ok': True, 'selected_profile': name,
                      'selection_state': 'selected' if name is not None else 'not_selected'}
        else:
            change = selector.select(args.profile_name)
            result = {'ok': True, 'selected_profile': change.selected_profile, 'changed': change.changed,
                      'selection_state': 'selected'}
    except RunBusy as exc:
        print(json.dumps({'ok': False, 'error': 'run_lock_busy', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ProfileError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': 'profile_state_error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result.update(state_file=str(selector.state_path), lock_file=str(selector.lock_path),
                  scope='4A_selector_storage_only', runtime_profile_reported=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hma", description="Helpdesk Mail Agent")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="đường dẫn agent.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init-db", help="tạo CSDL, ticket seed và nạp KB")
    a.add_argument("--force", action="store_true", help="xóa CSDL cũ và dựng lại")
    a.set_defaults(func=_cmd_init_db)
    m = sub.add_parser("migrate-db", help="nâng schema CSDL hiện có; không dựng lại dữ liệu")
    m.set_defaults(func=_cmd_migrate_db)
    b = sub.add_parser("kb-search", help="thử tìm kiếm KB")
    b.add_argument("query")
    b.add_argument("--top-k", type=int, default=0)
    b.set_defaults(func=_cmd_kb_search)
    c = sub.add_parser("status", help="đếm bản ghi")
    c.set_defaults(func=_cmd_status)
    d = sub.add_parser("a0", help="nghiệm thu A0 trên hạ tầng thật (IMAP/SMTP)")
    d.add_argument("--move-test", action="store_true", help="thử APPEND + chuyển một thư thử vào Archive")
    d.add_argument("--send-test", choices=["client", "partner"],
                   help="gửi một thư thử tới quản trị của loại nguồn này (alice hoặc bob)")
    d.set_defaults(func=_cmd_a0)
    from .profiles import PROFILE_NAMES
    profile = sub.add_parser('profile', help='xem/đổi lựa chọn profile; 4A chưa kích hoạt policy')
    profile_sub = profile.add_subparsers(dest='profile_command', required=True)
    show = profile_sub.add_parser('show', help='xem lựa chọn; không tự tạo mặc định')
    show.set_defaults(func=_cmd_profile)
    select = profile_sub.add_parser('select', help='lưu lựa chọn dưới cùng khóa với runner live')
    select.add_argument('profile_name', choices=PROFILE_NAMES)
    select.set_defaults(func=_cmd_profile)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except (ConfigError, kb.KBError, db.DbError, MailError, FileExistsError, ValueError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2
    except Exception:
        log.exception("lỗi không xử lý được")
        return 1
