"""Gộp nhiều báo cáo khi chạy thật chia nấc (giai đoạn 6)."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from hma.evaluate import evaluate_report, main, merge_reports

SET = {"mails": [
    {"id": "M04", "from": "customer1@customer.test", "subject": "Giờ làm việc?",
     "expect": {"task_type": 2, "reply_to": "customer1@customer.test", "reply_contains": ["08:00"], "forbidden": ["create_ticket"]}},
    {"id": "M16", "from": "partner1@partner.test", "subject": "Bản tin",
     "expect": {"task_type": 6, "moved_to": "Archive", "forbidden": ["reply_message"]}},
    {"id": "M03", "from": "bob@corp.test", "subject": "FYI", "expect_skipped": True},
]}


def entry(sender, subject, status="DONE", **kw):
    e = {"uid": 1, "sender": sender, "subject": subject, "status": status, "reason": "", "task_type": None, "tickets": [],
         "actions": [], "replies": [], "handoffs": [], "archived": False}
    e.update(kw)
    return e


M04 = entry("customer1@customer.test", "Giờ làm việc?", task_type=2,
            replies=[{"to": "customer1@customer.test", "subject": "Re:", "body": "08:00 đến 17:00"}])
M16 = entry("partner1@partner.test", "Bản tin", task_type=6, archived=True)
M03 = entry("bob@corp.test", "FYI", status="SKIPPED")


class MergeTests(unittest.TestCase):
    def test_partial_reports_are_incomplete_alone_but_complete_together(self):
        a, b = {"mode": "live", "messages": [M04]}, {"mode": "live", "messages": [M16, M03]}
        self.assertEqual(evaluate_report(a, SET)["summary"]["missing"], ["M16", "M03"])
        merged = merge_reports([a, b])
        self.assertEqual(merged["mode"], "live")
        s = evaluate_report(merged, SET)["summary"]
        self.assertEqual((s["passed"], s["scored"], s["missing"], s["ok"]), (2, 2, [], True))
        self.assertEqual((s["skipped_ok"], s["forbidden"]), (1, 0))

    def test_later_report_wins_for_the_same_mail(self):
        failed = entry("customer1@customer.test", "Giờ làm việc?", status="FAILED", reason="mạng")
        merged = merge_reports([{"mode": "live", "messages": [failed]}, {"mode": "live", "messages": [M04]}])
        self.assertEqual(len(merged["messages"]), 1)
        self.assertEqual(merged["messages"][0]["status"], "DONE")

    def test_subject_with_lost_space_is_the_same_mail(self):
        a = entry("partner1@partner.test", "Thông báo: bảo trì phía đối tác", status="DONE")
        b = entry("partner1@partner.test", "Thông báo: bảo trì phíađối tác", status="SKIPPED")
        self.assertEqual(len(merge_reports([{"messages": [a]}, {"messages": [b]}])["messages"]), 1)

    def test_modes_are_listed(self):
        m = merge_reports([{"mode": "live", "messages": []}, {"mode": "dry-run", "messages": []}])
        self.assertEqual(m["mode"], "dry-run+live")

    def test_cli_accepts_several_reports(self):
        with tempfile.TemporaryDirectory() as t:
            p1, p2, ps = Path(t) / "1.json", Path(t) / "2.json", Path(t) / "set.json"
            p1.write_text(json.dumps({"mode": "live", "messages": [M04]}, ensure_ascii=False), encoding="utf-8")
            p2.write_text(json.dumps({"mode": "live", "messages": [M16, M03]}, ensure_ascii=False), encoding="utf-8")
            ps.write_text(json.dumps(SET, ensure_ascii=False), encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main([str(p1), str(p2), "--set", str(ps)])
            out = buf.getvalue()
            self.assertIn("1.json, ", out)
            self.assertIn("2.json", out)
            self.assertIn("Thư có đáp án: 2/2 đạt", out)
            self.assertNotIn("Không có trong báo cáo", out)
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
