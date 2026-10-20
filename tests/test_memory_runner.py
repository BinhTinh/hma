"""3A1: context, report và partial effects của runner. Không dùng LLM/SMTP thật."""
from __future__ import annotations
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from hma import db, runner, store
from hma.memory_context import MemoryExecutionContext
from hma.port import CapturingPort
from hma.tools import ToolSettings

SETTINGS=ToolSettings({"customer.test":"client","partner.test":"partner"},
                      {"client":"alice@corp.test","partner":"bob@corp.test"},3,20)


def raw_mail(uid):
    msg=EmailMessage()
    msg['From']='customer1@customer.test'
    msg['To']='support@corp.test'
    msg['Return-Path']='<customer1@customer.test>'
    msg['Subject']='Về TKT-0001'
    msg['Message-ID']=f'<runner-memory-{uid}@customer.test>'
    msg.set_content('TKT-0001: bổ sung thông tin đã thử đăng nhập lại.')
    return msg.as_bytes()


def done():
    usage=SimpleNamespace(prompt_tokens=0,completion_tokens=0,reasoning_tokens=0,cache_hit_tokens=0,total_tokens=0)
    return SimpleNamespace(steps=1,usage=usage,outcome='DONE',reason='',status='done')


class MemoryRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'runner.db'
        db.init_db(self.path)
        self.conn=db.connect(self.path)
        self.addCleanup(self.conn.close)
        limits=runner.Limits(8,30,1000,10000,10)
        self.env=runner.Env(self.conn,CapturingPort(None),object(),SETTINGS,'SYS',limits,uidvalidity=111)

    def test_default_env_has_no_profile_and_advertises_old_tools(self):
        self.assertIsNone(self.env.memory)
        def episode(llm,tb,schemas,*args,**kwargs):
            self.assertIsNone(tb.ctx.memory)
            self.assertEqual(len(schemas),8)
            self.assertNotIn('append_ticket_note',{s['function']['name'] for s in schemas})
            tb.call('set_task_type',{'task_type':1})
            return done()
        with patch('hma.runner.run_episode',side_effect=episode):
            entry=runner.process_uid(self.env,7,raw_mail(7),None)
        self.assertEqual(entry['status'],'DONE')
        self.assertEqual(entry['ticket_notes_written'],[])
        self.assertEqual(entry['ticket_notes_read'],[])

    def test_context_is_shared_across_emails_and_report_tracks_writes(self):
        memory=MemoryExecutionContext('two-email-fixture','protected')
        self.env.memory=memory
        def episode(llm,tb,schemas,*args,**kwargs):
            self.assertIs(tb.ctx.memory,memory)
            self.assertEqual(len(schemas),9)
            self.assertTrue(tb.call('set_task_type',{'task_type':4})['ok'])
            self.assertTrue(tb.call('update_ticket',{'ticket_id':'TKT-0001','status':'pending'})['ok'])
            result=tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Thông tin bổ sung.'})
            self.assertTrue(result['ok'])
            return done()
        with patch('hma.runner.run_episode',side_effect=episode):
            entries=[runner.process_uid(self.env,uid,raw_mail(uid),None) for uid in (7,8)]
        for entry in entries:
            self.assertEqual(entry['status'],'DONE')
            self.assertEqual(len(entry['ticket_notes_written']),1)
            note=entry['ticket_notes_written'][0]
            self.assertEqual(note['run_id'],memory.run_id)
            self.assertEqual(note['profile'],memory.profile)
            self.assertEqual(note['source_message_key'],entry['key'])

    def test_note_alone_is_detected_as_effect(self):
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=7,
                                  sender='customer1@customer.test',sender_class='client',subject='x',truncated=False)
        self.assertFalse(runner._has_effects(self.conn,key))
        with db.transaction(self.conn):
            store.append_ticket_note(self.conn,ticket_id='TKT-0001',body='Một ghi chú',
                                     source_message_key=key,run_id='fixture',profile='protected')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM actions').fetchone()[0],0)
        self.assertTrue(runner._has_effects(self.conn,key))

    def test_exception_after_note_is_partial_effect_not_retryable_failed(self):
        self.env.memory=MemoryExecutionContext('failure-fixture','protected')
        def episode(llm,tb,schemas,*args,**kwargs):
            tb.call('set_task_type',{'task_type':4})
            self.assertTrue(tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Đã ghi trước lỗi.'})['ok'])
            raise RuntimeError('mô phỏng lỗi sau ghi chú')
        with patch('hma.runner.run_episode',side_effect=episode):
            entry=runner.process_uid(self.env,7,raw_mail(7),None)
        self.assertEqual(entry['status'],'NEEDS_HUMAN')
        self.assertIn('partial_effects',entry['reason'])
        self.assertEqual(len(entry['ticket_notes_written']),1)

    def test_read_trace_is_recorded_without_exposing_runtime_metadata_to_model(self):
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=99,
                                  sender='customer1@customer.test',sender_class='client',subject='old',truncated=False)
        with db.transaction(self.conn):
            note=store.append_ticket_note(self.conn,ticket_id='TKT-0001',body='Bối cảnh cũ',
                                          source_message_key=key,run_id='old-fixture',profile='protected')
        def episode(llm,tb,schemas,*args,**kwargs):
            result=tb.call('get_ticket',{'ticket_id':'TKT-0001'})
            self.assertTrue(result['ok'])
            self.assertEqual(set(result['notes'][0]),{'id','body','created_at'})
            tb.call('set_task_type',{'task_type':1})
            return done()
        with patch('hma.runner.run_episode',side_effect=episode):
            entry=runner.process_uid(self.env,7,raw_mail(7),None)
        self.assertEqual(entry['ticket_notes_read'],[{'ticket_id':'TKT-0001','note_ids':[note['id']],
                                                      'source_message_keys':[key]}])
        self.assertEqual(entry['ticket_notes_written'],[])

    def test_missing_context_does_not_create_note_when_called_directly(self):
        def episode(llm,tb,schemas,*args,**kwargs):
            tb.call('set_task_type',{'task_type':1})
            result=tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Không được ghi'})
            self.assertEqual(result['error'],'memory_context_required')
            return done()
        with patch('hma.runner.run_episode',side_effect=episode):
            entry=runner.process_uid(self.env,7,raw_mail(7),None)
        self.assertEqual(entry['ticket_notes_written'],[])


if __name__=='__main__':
    unittest.main()
