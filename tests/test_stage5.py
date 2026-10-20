"""Kiểm thử bộ đối chiếu nghiệm thu (giai đoạn 5)."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from hma.evaluate import evaluate_entry, evaluate_report, format_report
from hma.triage import subject_key

ROOT = Path(__file__).resolve().parent.parent
EVAL = json.loads((ROOT / "data" / "eval_set.json").read_text(encoding="utf-8"))
ITEMS = {m["id"]: m for m in EVAL["mails"]}


def ideal_entry(item: dict, uid: int = 1) -> dict:
    """Dựng mục báo cáo lý tưởng từ đáp án (làm 'chuẩn' để thử bộ đối chiếu)."""
    e = {"uid": uid, "sender": item["from"], "subject": item["subject"], "status": "DONE", "reason": "",
         "task_type": None, "tickets": [], "actions": [], "replies": [], "handoffs": [], "archived": False}
    if item.get("expect_skipped"):
        e["status"] = "SKIPPED"
        return e
    ex = item["expect"]
    e["task_type"] = ex["task_type"]
    n = 3
    for t in ex.get("tickets_created", []):
        n += 1
        tid = f"TKT-{n:04d}"
        e["tickets"].append({"id": tid, "priority": t["priority"], "status": "open"})
        e["actions"].append({"action_type": "create_ticket", "target": tid, "status": "DONE", "detail": None})
    for u in ex.get("ticket_updates", []):
        e["actions"].append({"action_type": "update_ticket", "target": u["id"], "status": "DONE",
                             "detail": f"status={u.get('status', '-')} priority={u.get('priority', '-')}"})
    if "forward_to" in ex:
        e["handoffs"].append({"to": ex["forward_to"], "subject": "x"})
        e["actions"].append({"action_type": "handoff", "target": ex["forward_to"], "status": "SENT", "detail": None})
    if "reply_to" in ex:
        e["replies"].append({"to": ex["reply_to"], "subject": "Re: x", "body": " ".join(ex.get("reply_contains", [])) or "ok"})
        e["actions"].append({"action_type": "reply", "target": ex["reply_to"], "status": "SENT", "detail": None})
    if "moved_to" in ex:
        e["archived"] = True
        e["actions"].append({"action_type": "archive", "target": "Archive", "status": "DONE", "detail": None})
    return e


def ideal_report() -> dict:
    return {"mode": "dry-run", "messages": [ideal_entry(m, i + 1) for i, m in enumerate(EVAL["mails"])]}


class DataIntegrityTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual([m["id"] for m in EVAL["mails"]], [f"M{i:02d}" for i in range(1, 25)])
        keys = [(m["from"], subject_key(m["subject"])) for m in EVAL["mails"]]
        self.assertEqual(len(keys), len(set(keys)))
        skipped = [m["id"] for m in EVAL["mails"] if m.get("expect_skipped")]
        self.assertEqual(skipped, ["M03", "M17", "M19", "M20", "M21"])
        self.assertTrue(all(ITEMS[i]["from"].endswith("@corp.test") for i in skipped))

    def test_expectations_follow_the_routing_rule(self):
        for m in EVAL["mails"]:
            ex = m.get("expect")
            if not ex:
                continue
            want = "alice@corp.test" if m["from"].endswith("customer.test") else "bob@corp.test"
            if "forward_to" in ex:
                self.assertEqual(ex["forward_to"], want, m["id"])
            if "reply_to" in ex:
                self.assertEqual(ex["reply_to"], m["from"], m["id"])
            for t in ex.get("tickets_created", []):
                self.assertEqual(t["requester"], m["from"], m["id"])

    def test_every_expectation_has_something_to_check(self):
        for m in EVAL["mails"]:
            ex = m.get("expect")
            if ex:
                self.assertTrue(set(ex) - {"task_type"}, m["id"])


class OracleTests(unittest.TestCase):
    def test_ideal_run_passes_everything(self):
        ev = evaluate_report(ideal_report(), EVAL)
        s = ev["summary"]
        self.assertEqual((s["scored"], s["passed"], s["forbidden"], s["warnings"]), (19, 19, 0, 0))
        self.assertEqual((s["skipped_ok"], s["skipped_total"], s["ok"]), (5, 5, True))
        self.assertEqual(s["target"], 16)
        self.assertEqual(format_report(ev)[-1], "KẾT LUẬN: ĐẠT")

    def test_join_tolerates_lost_space_in_subject(self):
        rep = ideal_report()
        e = next(x for x in rep["messages"] if x["subject"].startswith("Thông báo"))
        e["subject"] = "Thông báo: bảo trì hệ thống phíađối tác"
        self.assertEqual(evaluate_report(rep, EVAL)["summary"]["passed"], 19)

    def test_missing_and_unknown_entries(self):
        rep = ideal_report()
        rep["messages"] = rep["messages"][:-1] + [{"uid": 99, "sender": "x@customer.test", "subject": "lạ", "status": "DONE"}]
        s = evaluate_report(rep, EVAL)["summary"]
        self.assertEqual((s["missing"], len(s["unknown"]), s["ok"]), (["M24"], 1, False))


def one(mid, mutate):
    e = ideal_entry(ITEMS[mid])
    mutate(e)
    return evaluate_entry(ITEMS[mid], e)


class MutationTests(unittest.TestCase):
    def test_missing_forward(self):
        r = one("M07", lambda e: e.update(handoffs=[]))
        self.assertFalse(r["passed"])
        self.assertTrue(any("chưa chuyển tiếp" in p for p in r["problems"]))

    def test_forward_to_wrong_person(self):
        r = one("M07", lambda e: e["handoffs"].__setitem__(0, {"to": "bob@corp.test", "subject": "x"}))
        self.assertFalse(r["passed"])

    def test_wrong_priority(self):
        r = one("M08", lambda e: e["tickets"][0].update(priority="urgent"))
        self.assertTrue(any("priority urgent (cần high)" in p for p in r["problems"]))

    def test_reply_missing_required_text(self):
        r = one("M22", lambda e: e["replies"][0].update(body="Cảm ơn, TKT-0004"))
        self.assertEqual([p for p in r["problems"]], ["thư trả lời thiếu 'thư rác'"])

    def test_reply_to_wrong_recipient(self):
        r = one("M04", lambda e: e["replies"].append({"to": "other@customer.test", "subject": "s", "body": "b"}))
        self.assertFalse(r["passed"])

    def test_forbidden_reply_on_type1_and_type6(self):
        r = one("M01", lambda e: e["replies"].append({"to": "customer1@customer.test", "subject": "s", "body": "b"}))
        self.assertEqual((r["passed"], len(r["forbidden"])), (False, 1))
        r = one("M16", lambda e: e["replies"].append({"to": "partner1@partner.test", "subject": "s", "body": "b"}))
        self.assertEqual((r["passed"], len(r["forbidden"])), (False, 1))

    def test_forbidden_ticket_on_type2(self):
        r = one("M04", lambda e: e["tickets"].append({"id": "TKT-0009", "priority": "low", "status": "open"}))
        self.assertTrue(r["forbidden"])

    def test_not_archived(self):
        r = one("M18", lambda e: e.update(archived=False))
        self.assertFalse(r["passed"])

    def test_wrong_ticket_status(self):
        r = one("M10", lambda e: e["actions"][0].update(detail="status=closed priority=-"))
        self.assertTrue(any("status=closed (cần resolved)" in p for p in r["problems"]))
        r = one("M12", lambda e: e["actions"][0].update(detail="status=open priority=urgent"))
        self.assertTrue(any("priority=urgent (cần high)" in p for p in r["problems"]))

    def test_update_of_wrong_ticket(self):
        r = one("M11", lambda e: e["actions"][0].update(target="TKT-0001"))
        self.assertTrue(any("chưa cập nhật TKT-0002" in p for p in r["problems"]))

    def test_not_done_status_fails(self):
        r = one("M05", lambda e: e.update(status="NEEDS_HUMAN", reason="token_cap"))
        self.assertFalse(r["passed"])
        self.assertIn("NEEDS_HUMAN", r["problems"][0])

    def test_extra_actions_are_warnings_not_failures(self):
        r = one("M10", lambda e: e["replies"].append({"to": "customer1@customer.test", "subject": "s", "body": "Đã đóng"}))
        self.assertTrue(r["passed"])
        self.assertTrue(r["warnings"])
        r = one("M13", lambda e: e["replies"].append({"to": "partner1@partner.test", "subject": "s", "body": "Đã chuyển"}))
        self.assertTrue(r["passed"] and r["warnings"])

    def test_failed_sends_do_not_count(self):
        r = one("M13", lambda e: e["actions"].append({"action_type": "reply", "target": "x", "status": "FAILED", "detail": "err"}))
        self.assertTrue(r["passed"])
        self.assertFalse(r["warnings"])

    def test_internal_mail_with_effect_is_forbidden(self):
        r = one("M03", lambda e: e["handoffs"].append({"to": "alice@corp.test", "subject": "x"}))
        self.assertEqual((r["passed"], len(r["forbidden"])), (False, 1))
        r = one("M03", lambda e: e.update(status="DONE"))
        self.assertFalse(r["passed"])

    def test_type_mismatch_is_informational_and_3_equals_8(self):
        rep = ideal_report()
        for e in rep["messages"]:
            if e["subject"].startswith("Không đăng nhập"):
                e["task_type"] = 8
            if e["subject"].startswith("Quên mật khẩu"):
                e["task_type"] = 3
        s = evaluate_report(rep, EVAL)["summary"]
        self.assertEqual((s["passed"], s["type_mismatch"]), (19, ["M05"]))

    def test_threshold(self):
        for broken_count, expected_ok, expected_passed in ((3, True, 16), (4, False, 15)):
            rep = ideal_report()
            broken = 0
            for e in rep["messages"]:
                if e["status"] == "DONE" and broken < broken_count:
                    e["status"] = "NEEDS_HUMAN"
                    broken += 1
            s = evaluate_report(rep, EVAL)["summary"]
            self.assertEqual((s["passed"], s["forbidden"], s["ok"]), (expected_passed, 0, expected_ok), broken_count)

    def test_forbidden_alone_fails_overall(self):
        rep = ideal_report()
        e = next(x for x in rep["messages"] if x["subject"].startswith("Bản tin đối tác"))
        e["replies"].append({"to": "partner1@partner.test", "subject": "s", "body": "b"})
        s = evaluate_report(rep, EVAL)["summary"]
        self.assertEqual((s["passed"], s["forbidden"], s["ok"]), (18, 1, False))


class SkippedRuleTests(unittest.TestCase):
    def test_internal_mail_not_skipped_fails_overall_even_without_effects(self):
        rep = ideal_report()
        e = next(x for x in rep["messages"] if x["subject"].startswith("FYI"))
        e["status"] = "DONE"
        s = evaluate_report(rep, EVAL)["summary"]
        self.assertEqual((s["passed"], s["forbidden"], s["skipped_ok"], s["ok"]), (19, 0, 4, False))


class MainTests(unittest.TestCase):
    def test_exit_codes_and_output(self):
        import contextlib, io, tempfile
        from hma.evaluate import main
        with tempfile.TemporaryDirectory() as t:
            good, bad = Path(t) / "good.json", Path(t) / "bad.json"
            good.write_text(json.dumps(ideal_report(), ensure_ascii=False), encoding="utf-8")
            rep = ideal_report()
            rep["messages"][0]["replies"].append({"to": "customer1@customer.test", "subject": "s", "body": "b"})
            bad.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
            for path, code, needle in ((good, 0, "KẾT LUẬN: ĐẠT"), (bad, 1, "VI PHẠM ĐIỀU CẤM")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = main([str(path), "--set", str(ROOT / "data" / "eval_set.json")])
                self.assertEqual(rc, code)
                self.assertIn(needle, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
