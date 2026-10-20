"""4B metadata trên runner/loop thật, FakeLLM và SQLite tạm của stage4."""
from __future__ import annotations
import json
from pathlib import Path
from hma import health, runner
from hma.memory_context import MemoryExecutionContext
from hma.runtime_binding import context_metadata
from test_stage4 import Base, FakeLLM, FakeSource, raw_mail, type2_flow


class RunnerBindingMetadataTests(Base):
    def setUp(self):
        super().setUp()
        self.memory=MemoryExecutionContext('metadata-fixture','protected')

    def env(self,llm,**kwargs):
        env=super().env(llm,**kwargs)
        env.memory=self.memory
        return env

    def mails(self):
        return {1:raw_mail('customer1@customer.test','Giờ làm việc?','Bộ phận hỗ trợ làm việc giờ nào?')}

    def test_all_emails_in_batch_have_same_run_and_profile(self):
        mails=self.mails()
        mails[2]=raw_mail('customer1@customer.test','Giờ làm việc lần nữa','Xin giờ hỗ trợ.')
        entries=self.go(mails,FakeLLM(type2_flow()*2))
        self.assertEqual(len(entries),2)
        for entry in entries:
            self.assertEqual(entry['run_id'],self.memory.run_id)
            self.assertEqual(entry['profile'],self.memory.profile)
            self.assertEqual(entry['profile_policy_stage'],'binding_only')

    def test_skipped_email_also_has_context_without_model_call(self):
        llm=FakeLLM([])
        entries=self.go({1:raw_mail('alice@corp.test','FYI','Thông tin nội bộ.')},llm)
        self.assertEqual(entries[0]['status'],'SKIPPED')
        self.assertEqual(entries[0]['run_id'],self.memory.run_id)
        self.assertEqual(llm.calls,0)

    def test_profile_change_does_not_reexecute_terminal_mail(self):
        self.go(self.mails(),FakeLLM(type2_flow()))
        self.memory=MemoryExecutionContext('next-run','vulnerable')
        llm=FakeLLM([])
        self.assertEqual(self.go(self.mails(),llm),[])
        self.assertEqual(llm.calls,0)

    def test_report_header_and_entries_match_context(self):
        entries=self.go(self.mails(),FakeLLM(type2_flow()))
        path=runner.write_report(Path(self.tmp.name)/'report.json','dry-run',entries,'start',memory=self.memory)
        doc=json.loads(path.read_text())
        self.assertEqual(doc['run_id'],self.memory.run_id)
        self.assertEqual(doc['profile'],self.memory.profile)
        self.assertEqual(doc['messages'][0]['run_id'],doc['run_id'])

    def test_legacy_report_signature_still_works_without_context(self):
        entry={'uid':1,'status':'DONE','tokens':{'total':1}}
        path=runner.write_report(Path(self.tmp.name)/'legacy.json','dry-run',[entry],'start')
        doc=json.loads(path.read_text())
        self.assertNotIn('run_id',doc)
        self.assertEqual(doc['summary']['messages'],1)

    def test_report_refuses_mixed_identity_before_creating_file(self):
        entry={'uid':1,'status':'DONE','tokens':{},**context_metadata(self.memory)}
        path=Path(self.tmp.name)/'mixed'/'report.json'
        for changed in (dict(entry,run_id='other'),dict(entry,profile='vulnerable')):
            with self.assertRaises(runner.RunnerError):
                runner.write_report(path,'dry-run',[entry,changed],'start',memory=self.memory)
        self.assertFalse(path.exists())

    def test_idle_heartbeat_and_report_share_context(self):
        entries=self.go(self.mails(),FakeLLM(type2_flow()))
        hb=Path(self.tmp.name)/'hb.json'
        report=runner.finish_pass(entries,mode='live',started='s',report_dir=Path(self.tmp.name)/'logs',
                                  stamp='X',heartbeat_path=hb,memory=self.memory)
        document=json.loads(report.read_text())
        beat=health.read_heartbeat(hb)
        self.assertEqual((beat['profile'],beat['run_id']),(document['profile'],document['run_id']))
        self.assertEqual(beat['state'],'idle')
        self.assertEqual(health.check(hb,300)[0],0)

    def test_empty_pass_has_context_heartbeat_but_no_junk_report(self):
        hb=Path(self.tmp.name)/'hb.json'
        logs=Path(self.tmp.name)/'empty-logs'
        report=runner.finish_pass([],mode='live',started='s',report_dir=logs,stamp='X',heartbeat_path=hb,memory=self.memory)
        self.assertIsNone(report)
        self.assertFalse(logs.exists())
        beat=health.read_heartbeat(hb)
        self.assertEqual((beat['run_id'],beat['processed']),(self.memory.run_id,0))

    def test_dry_empty_finish_still_has_no_heartbeat_or_report(self):
        folder=Path(self.tmp.name)/'empty-dry'
        runner.finish_pass([],mode='dry-run',started='s',report_dir=folder,stamp='X',heartbeat_path=None,memory=self.memory)
        self.assertFalse(folder.exists())

    def test_context_drift_at_start_is_rejected_before_fetch_or_model(self):
        llm=FakeLLM([])
        env=self.env(llm)
        source=FakeSource(self.mails())
        def change(entries):
            env.memory=MemoryExecutionContext('changed','vulnerable')
        with self.assertRaises(runner.RunnerError):
            runner.run_batch(env,source,heartbeat=change,say=lambda text:None)
        self.assertEqual((source.fetched,llm.calls),([],0))

    def test_context_drift_after_last_email_is_not_silently_accepted(self):
        env=self.env(FakeLLM(type2_flow()))
        def change(entries):
            if entries:
                env.memory=MemoryExecutionContext('changed','vulnerable')
        with self.assertRaises(runner.RunnerError):
            runner.run_batch(env,FakeSource(self.mails()),heartbeat=change,say=lambda text:None)


if __name__=='__main__':
    import unittest
    unittest.main()
