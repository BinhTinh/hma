"""Đối chiếu báo cáo chạy (runner) với đáp án nghiệm thu (giai đoạn 5). Không cần mạng.

Chạy: python -m hma.evaluate logs/run-dry-run-XXXX.json [--set data/eval_set.json]
Trả mã thoát 0 khi: không có vi phạm điều cấm, 5 thư nội bộ đều SKIPPED và số thư đạt >= ngưỡng.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
from pathlib import Path

from .triage import subject_key

FORBID_MAP = {"reply_message": "reply", "create_ticket": "create_ticket", "forward_message": "handoff",
              "update_ticket": "update_ticket", "move_message": "archive"}
EFFECTS = ("reply", "create_ticket", "update_ticket", "handoff", "archive")
MIN_PASS_RATIO = 20 / 24     # ngưỡng do tôi đặt (tương đương 20/24); điều kiện cứng là 0 vi phạm điều cấm
EQUIVALENT_TYPES = ({3, 8},)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def effects(entry: dict) -> dict[str, bool]:
    """Những hành động ĐÃ xảy ra (gửi thành công, hoặc ghi CSDL không lỗi)."""
    done = {a["action_type"] for a in entry.get("actions", []) if a.get("status") != "FAILED"}
    return {"reply": bool(entry.get("replies")), "create_ticket": "create_ticket" in done or bool(entry.get("tickets")),
            "update_ticket": "update_ticket" in done, "handoff": bool(entry.get("handoffs")),
            "archive": bool(entry.get("archived"))}


def _parse_detail(detail: str | None) -> dict[str, str]:
    out = {}
    for part in (detail or "").split():
        k, _, v = part.partition("=")
        if v and v != "-":
            out[k] = v
    return out


def describe(entry: dict) -> str:
    bits = [entry.get("status", "?")]
    if entry.get("task_type") is not None:
        bits.append(f"loại {entry['task_type']}")
    for t in entry.get("tickets", []):
        bits.append(f"ticket {t['id']}/{t['priority']}")
    for a in entry.get("actions", []):
        if a["action_type"] == "update_ticket" and a.get("status") != "FAILED":
            bits.append(f"cập nhật {a['target']} ({a.get('detail')})")
    bits += [f"chuyển→{h['to'].split('@')[0]}" for h in entry.get("handoffs", [])]
    bits += [f"trả lời→{r['to'].split('@')[0]}" for r in entry.get("replies", [])]
    if entry.get("archived"):
        bits.append("archive")
    return ", ".join(bits)


def evaluate_entry(item: dict, entry: dict) -> dict:
    problems: list[str] = []
    forbidden: list[str] = []
    warnings: list[str] = []
    eff = effects(entry)
    res = {"id": item["id"], "uid": entry.get("uid"), "summary": describe(entry), "problems": problems,
           "forbidden": forbidden, "warnings": warnings, "expected_type": None, "type": entry.get("task_type")}

    if item.get("expect_skipped"):
        if entry.get("status") != "SKIPPED":
            problems.append(f"thư ngoài danh sách phải SKIPPED, nhận {entry.get('status')}")
        if any(eff.values()):
            forbidden.append("thư ngoài danh sách nhưng có hành động")
        res["passed"] = not problems and not forbidden
        return res

    ex = item["expect"]
    res["expected_type"] = ex.get("task_type")
    if entry.get("status") != "DONE":
        problems.append(f"trạng thái {entry.get('status')} ({entry.get('reason', '')})")

    flagged: set[str] = set()
    if ex.get("no_actions"):
        for name, on in eff.items():
            if on:
                forbidden.append(f"loại 1 không được có hành động nào, nhưng có {name}")
                flagged.add(name)
    for g in ex.get("forbidden", []):
        act = FORBID_MAP[g]
        if eff[act]:
            forbidden.append(f"bị cấm {g} nhưng đã xảy ra")
            flagged.add(act)

    required: set[str] = set()
    if "tickets_created" in ex:
        required.add("create_ticket")
        want, got = ex["tickets_created"], entry.get("tickets", [])
        if len(got) != len(want):
            problems.append(f"cần {len(want)} ticket mới, có {len(got)}")
        else:
            for w_, g_ in zip(want, got):
                if g_["priority"] != w_["priority"]:
                    problems.append(f"priority {g_['priority']} (cần {w_['priority']})")
                if entry.get("sender") != w_["requester"]:
                    problems.append(f"người yêu cầu {entry.get('sender')} (cần {w_['requester']})")
                for sub in w_.get("subject_contains", []):
                    if _nfc(sub).lower() not in _nfc(entry.get("subject", "")).lower():
                        problems.append(f"chủ đề ticket thiếu {sub!r}")
    if "ticket_updates" in ex:
        required.add("update_ticket")
        ups = {a["target"]: _parse_detail(a.get("detail")) for a in entry.get("actions", [])
               if a["action_type"] == "update_ticket" and a.get("status") != "FAILED"}
        for w_ in ex["ticket_updates"]:
            got_u = ups.get(w_["id"])
            if got_u is None:
                problems.append(f"chưa cập nhật {w_['id']}")
                continue
            for field in ("status", "priority"):
                if field in w_ and got_u.get(field) != w_[field]:
                    problems.append(f"{w_['id']}: {field}={got_u.get(field)} (cần {w_[field]})")
    if "reply_to" in ex:
        required.add("reply")
        replies = entry.get("replies", [])
        mine = [r for r in replies if r["to"] == ex["reply_to"]]
        if not mine:
            problems.append(f"thiếu thư trả lời tới {ex['reply_to']}")
        else:
            body = _nfc(mine[0]["body"])
            for s in ex.get("reply_contains", []):
                if _nfc(s) not in body:
                    problems.append(f"thư trả lời thiếu {s!r}")
        if any(r["to"] != ex["reply_to"] for r in replies):
            problems.append("có thư trả lời gửi nhầm người nhận")
    if "forward_to" in ex:
        required.add("handoff")
        targets = [h["to"] for h in entry.get("handoffs", [])]
        if ex["forward_to"] not in targets:
            problems.append(f"chưa chuyển tiếp tới {ex['forward_to']}")
        if any(t != ex["forward_to"] for t in targets):
            problems.append("có thư chuyển tiếp tới người không đúng")
    if "moved_to" in ex:
        required.add("archive")
        if not entry.get("archived"):
            problems.append(f"chưa chuyển vào {ex['moved_to']}")

    extra = [n for n, on in eff.items() if on and n not in required and n not in flagged]
    if extra:
        warnings.append("hành động thừa (không bắt buộc, không bị cấm): " + ", ".join(extra))
    res["passed"] = not problems and not forbidden
    return res


def _types_compatible(expected, got) -> bool:
    if expected is None or got is None or expected == got:
        return True
    return any({expected, got} <= group for group in EQUIVALENT_TYPES)


def evaluate_report(report: dict, eval_set: dict) -> dict:
    index = {(m["from"].lower(), subject_key(m["subject"])): m for m in eval_set["mails"]}
    results, unknown, seen = [], [], set()
    for entry in report["messages"]:
        item = index.get((entry["sender"].lower(), subject_key(entry["subject"])))
        if item is None:
            unknown.append(f"UID {entry.get('uid')} {entry['sender']} | {entry['subject']}")
            continue
        r = evaluate_entry(item, entry)
        if item["id"] in seen:
            r["warnings"].append("trùng: có nhiều hơn một mục báo cáo cho thư này")
        seen.add(item["id"])
        results.append(r)
    missing = [m["id"] for m in eval_set["mails"] if m["id"] not in seen]
    scored = [r for r in results if r["expected_type"] is not None]
    skipped_chk = [r for r in results if r["expected_type"] is None]
    summary = {
        "scored": len(scored), "passed": sum(r["passed"] for r in scored),
        "forbidden": sum(len(r["forbidden"]) for r in results),
        "skipped_ok": sum(r["passed"] for r in skipped_chk), "skipped_total": len(skipped_chk),
        "warnings": sum(len(r["warnings"]) for r in results),
        "type_mismatch": [r["id"] for r in scored if not _types_compatible(r["expected_type"], r["type"])],
        "missing": missing, "unknown": unknown,
    }
    summary["target"] = math.ceil(len(scored) * MIN_PASS_RATIO)
    summary["ok"] = (summary["forbidden"] == 0 and summary["skipped_ok"] == summary["skipped_total"]
                     and summary["passed"] >= summary["target"] and not missing and not unknown)
    return {"results": results, "summary": summary}


def merge_reports(reports: list[dict]) -> dict:
    # Gộp nhiều báo cáo (theo thứ tự thời gian). Cùng một thư xuất hiện nhiều lần thì bản sau thắng.
    merged: dict[tuple[str, str], dict] = {}
    for rep in reports:
        for e in rep["messages"]:
            merged[(e["sender"].lower(), subject_key(e["subject"]))] = e
    modes = sorted({str(r.get("mode", "?")) for r in reports})
    return {"mode": "+".join(modes), "messages": list(merged.values())}


def format_report(ev: dict) -> list[str]:
    lines = []
    for r in sorted(ev["results"], key=lambda x: x["id"]):
        mark = "ĐẠT " if r["passed"] else "TRƯỢT"
        lines.append(f"{r['id']} UID {r['uid']:>2} {mark} | {r['summary']}")
        for p in r["problems"]:
            lines.append(f"      - {p}")
        for f in r["forbidden"]:
            lines.append(f"      ! VI PHẠM ĐIỀU CẤM: {f}")
        for wmsg in r["warnings"]:
            lines.append(f"      ~ {wmsg}")
    s = ev["summary"]
    lines += ["", f"Thư có đáp án: {s['passed']}/{s['scored']} đạt (ngưỡng {s['target']}); "
                  f"thư nội bộ bỏ qua đúng: {s['skipped_ok']}/{s['skipped_total']}; "
                  f"vi phạm điều cấm: {s['forbidden']}; cảnh báo: {s['warnings']}"]
    if s["type_mismatch"]:
        lines.append("Loại tác vụ khác kỳ vọng (chỉ để tham khảo, 3 và 8 coi như tương đương): " + ", ".join(s["type_mismatch"]))
    if s["missing"]:
        lines.append("Không có trong báo cáo: " + ", ".join(s["missing"]))
    if s["unknown"]:
        lines.append("Không có trong đáp án: " + "; ".join(s["unknown"]))
    lines.append("KẾT LUẬN: " + ("ĐẠT" if s["ok"] else "CHƯA ĐẠT"))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hma.evaluate", description=__doc__)
    ap.add_argument("reports", nargs="+", help="một hoặc nhiều tệp JSON do hma.runner ghi ra (theo thứ tự thời gian)")
    ap.add_argument("--set", default="data/eval_set.json", dest="eval_set")
    args = ap.parse_args(argv)
    report = merge_reports([json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports])
    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    ev = evaluate_report(report, eval_set)
    print(f"Báo cáo: {', '.join(args.reports)} (chế độ {report.get('mode')}, {len(report['messages'])} thư)\n")
    print("\n".join(format_report(ev)))
    return 0 if ev["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
