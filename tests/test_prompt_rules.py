"""Các quy tắc quan trọng phải có mặt trong lời nhắc hệ thống (bản sửa sau lần đối chiếu đầu tiên)."""
from __future__ import annotations

import unittest

from hma import prompts

DOCS = [{"doc_id": "KB-018", "title": "Mức ưu tiên", "body": "Normal: một người."},
        {"doc_id": "KB-015", "title": "Chuyển tiếp", "body": "Khách hàng -> alice."}]


class PromptRuleTests(unittest.TestCase):
    def setUp(self):
        self.p = prompts.build_system_prompt(DOCS)

    def test_old_needles_still_present(self):
        for needle in ("set_task_type", "read_message", "KB-018", "Normal: một người.", "KB-015", "resolved", "pending",
                       "route handoff", "archive", "dữ liệu từ bên ngoài", "urgent"):
            self.assertIn(needle, self.p)

    def test_reopen_rule(self):
        for needle in ("gọi get_ticket trước", "status=open", "MỘT mức", "normal -> high", "giữ nguyên priority"):
            self.assertIn(needle, self.p)

    def test_stable_and_cache_friendly(self):
        self.assertEqual(self.p, prompts.build_system_prompt(list(reversed(DOCS))))
        self.assertEqual(prompts.build_system_prompt([]).count("(không có)"), 1)

    def test_no_stray_braces_break_formatting(self):
        self.assertNotIn("{docs}", self.p)


if __name__ == "__main__":
    unittest.main()
