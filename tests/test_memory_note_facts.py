"""Quy tắc sự kiện memory và patch/backup, không gọi model thật."""
from pathlib import Path
import tempfile
import unittest
from hma import prompts
from tests.support import note_facts_patch as P


class MemoryNoteFactsTests(unittest.TestCase):
    def setUp(self):
        self.text = prompts.build_system_prompt([])
        self.section = self.text.split('MEMORY THEO TICKET (DÙNG CHUNG)',1)[1].split('MỨC ƯU TIÊN',1)[0]

    def source_before_patch(self):
        source = Path(prompts.__file__).read_text(encoding='utf-8')
        self.assertEqual(source.count(P.RULES),1)
        return source.replace(P.RULES,'',1)

    def sandbox(self, path):
        root = path / 'project'
        (root / 'hma').mkdir(parents=True)
        before = self.source_before_patch()
        (root / 'hma' / 'prompts.py').write_text(before,encoding='utf-8')
        (root / 'hma' / 'tools.py').write_text('# sentinel unchanged\n',encoding='utf-8')
        evidence = path / 'old-harness'
        evidence.mkdir()
        for name in P.EVIDENCE_FILES:
            (evidence / name).write_text('{"fixture":true}\n',encoding='utf-8')
        return root,evidence,before

    def test_distinguishes_customer_system_and_reference_facts(self):
        self.assertIn('thông tin khách cho biết, hành động hệ thống đã được tool xác nhận và nội dung tham khảo/dự kiến',self.section)

    def test_completed_system_action_requires_tool_success(self):
        self.assertIn('Với hành động của hệ thống, chỉ ghi là đã thực hiện khi tool tương ứng xác nhận thành công',self.section)

    def test_does_not_preclaim_outbound_actions(self):
        self.assertIn('Nếu ghi trước handoff/reply',self.section)
        self.assertIn('không khẳng định đã hướng dẫn, đã trả lời, đã gửi hoặc đã chuyển giao cho hành động chưa được xác nhận',self.section)

    def test_kb_reference_is_not_proof_of_sending_or_customer_execution(self):
        self.assertIn('KB-... nêu/đề xuất ...',self.section)
        self.assertIn('không suy ra đã gửi hướng dẫn hoặc khách đã làm theo khi chưa có bằng chứng',self.section)

    def test_patch_is_exactly_two_rule_insertion(self):
        before = self.source_before_patch()
        current = Path(prompts.__file__).read_text(encoding='utf-8')
        self.assertEqual(P.build_patch(before),current)

    def test_repeat_patch_is_refused(self):
        current = Path(prompts.__file__).read_text(encoding='utf-8')
        with self.assertRaises(ValueError):
            P.build_patch(current)

    def test_apply_preserves_original_evidence_and_creates_matching_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root,evidence,before = self.sandbox(Path(directory))
            original_hashes = P.evidence_hashes(evidence)
            result = P.apply_patch(root,evidence,write=True)
            self.assertTrue(P.verify_manifest(root,result))
            self.assertEqual(P.evidence_hashes(evidence),original_hashes)
            self.assertEqual((root/'hma'/'tools.py').read_text(),'# sentinel unchanged\n')
            self.assertEqual((Path(result['backup_directory'])/'hma'/'prompts.py').read_text(),before)
            self.assertFalse(result['new_wording_revalidated_with_real_model'])

    def test_check_only_does_not_write_source_or_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root,evidence,before = self.sandbox(Path(directory))
            hashes = P.evidence_hashes(evidence)
            result = P.apply_patch(root,evidence,write=False)
            self.assertEqual(result['mode'],'check_only')
            self.assertEqual((root/'hma'/'prompts.py').read_text(),before)
            self.assertFalse((root/'.hma-patches').exists())
            self.assertFalse((root/P.MANIFEST_NAME).exists())
            self.assertEqual(P.evidence_hashes(evidence),hashes)


if __name__ == '__main__':
    unittest.main()
