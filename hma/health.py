"""Dấu "còn sống" của dịch vụ (giai đoạn 7a): runner ghi sau mỗi thư và mỗi lượt; lệnh kiểm tra đọc lại.

Chạy kiểm tra: python -m hma.health [--max-age 300]   (mã thoát 0 ổn, 1 quá hạn, 2 không đọc được)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable


def write_heartbeat(path: str | Path, **info) -> None:
    """Ghi nguyên tử (ghi tệp tạm rồi đổi tên) để bên đọc không bao giờ thấy nửa chừng."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"epoch": time.time(), "time": time.strftime("%Y-%m-%dT%H:%M:%S"), **info}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_heartbeat(path: str | Path) -> dict | None:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def check(path: str | Path, max_age_s: float, now: Callable[[], float] = time.time) -> tuple[int, str]:
    doc = read_heartbeat(path)
    try:
        age = now() - float(doc["epoch"]) if doc else None
    except (KeyError, TypeError, ValueError):
        age = None
    if age is None:
        return 2, f"không đọc được dấu còn sống tại {path}"
    state = doc.get("state")
    if age > max_age_s:
        return 1, f"QUÁ HẠN: cập nhật cuối {age:.0f}s trước (giới hạn {max_age_s:.0f}s), trạng thái cuối: {state}"
    extra = " | ngân sách token ngày đã hết" if doc.get("budget_exhausted") else ""
    return 0, f"ổn: cập nhật {age:.0f}s trước, trạng thái {state}, lượt cuối xử lý {doc.get('processed', '?')} thư{extra}"


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m hma.health", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/agent.yaml")
    ap.add_argument("--max-age", type=float, default=300.0, help="số giây tối đa kể từ lần cập nhật cuối")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    code, msg = check(cfg.paths.db.parent / "heartbeat.json", args.max_age)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
