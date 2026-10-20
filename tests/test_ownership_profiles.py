"""4C1 ownership theo profile; SQLite và mail port giả, không payload/pentest/model thật."""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from hma import db, policy, store
from hma.memory_context import MemoryExecutionContext
from hma.profile_policy import policy_for_profile
from hma.tools import MessageContext, ToolSettings, Toolbox, TOOL_SCHEMAS
from hma.triage import ParsedMail

SETTINGS=ToolSettings({'customer.test':'client','partner.test':'partner'},
                      {'client':'alice@corp.test','partner':'bob@corp.test'},3,20)


class NoMail:
    def send_reply(self,**kwargs):
        raise AssertionError('không gửi mail thật')
    def send_handoff(self,**kwargs):
        raise AssertionError('không gửi mail thật')
    def archive(self,uid):
        raise AssertionError('không đổi mailbox thật')


class OwnershipProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'ownership.db'
        db.init_db(path)
        self.conn=db.connect(path)
        self.addCleanup(self.conn.close)

    def box(self,name,uid=7,*,sender='partner1@partner.test',ticket='TKT-0001',mentioned=True):
        text=f'Bổ sung thông tin cho {ticket}' if mentioned else 'Tôi cần hỗ trợ.'
        mail=ParsedMail(sender=sender,return_path=sender,return_path_present=True,
                        subject=text,message_id=f'<{uid}@unit.test>',references=None,
                        auto_submitted=None,precedence=None,has_list_header=False,
                        body_text=text,truncated=False)
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=uid,sender=sender,
                                   sender_class='client' if sender.endswith('customer.test') else 'partner',
                                   subject=text,truncated=False)
        settings=replace(SETTINGS,profile_policy=policy_for_profile(name))
        memory=MemoryExecutionContext(f'owner-{name}-{uid}',name)
        return Toolbox(MessageContext(self.conn,key,uid,mail,settings,NoMail(),memory=memory))

    def seed_note(self):
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=99,
                                   sender='customer1@customer.test',sender_class='client',subject='old',truncated=False)
        with db.transaction(self.conn):
            return store.append_ticket_note(self.conn,ticket_id='TKT-0001',body='Khách đã thử đăng nhập lại.',
                                            source_message_key=key,run_id='seed',profile='protected')

    def test_own_ticket_read_is_allowed_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=10+index,sender='customer1@customer.test')
            self.assertTrue(tb.call('get_ticket',{'ticket_id':'TKT-0001'})['ok'])

    def test_protected_foreign_read_denies_before_notes_are_returned(self):
        self.seed_note()
        tb=self.box('protected')
        result=tb.call('get_ticket',{'ticket_id':'TKT-0001'})
        self.assertEqual(result['error'],'ticket_not_found')
        self.assertNotIn('notes',result)
        self.assertEqual(tb.memory_reads,[])
        event=tb.ownership_decisions[-1]
        self.assertEqual(event['enforcement'],'enforce')
        self.assertTrue(event['ownership_condition_would_block'])
        self.assertFalse(event['policy_precheck_allowed'])

    def test_vulnerable_foreign_read_observes_and_returns_common_public_shape(self):
        self.seed_note()
        tb=self.box('vulnerable')
        result=tb.call('get_ticket',{'ticket_id':'TKT-0001'})
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['notes']),1)
        self.assertNotIn('requester',result['ticket'])
        self.assertEqual(set(result['notes'][0]),{'id','body','created_at'})
        self.assertNotIn('ownership_decisions',result)
        event=tb.ownership_decisions[-1]
        self.assertEqual(event['enforcement'],'observe_only')
        self.assertTrue(event['ownership_condition_would_block'])
        self.assertTrue(event['policy_precheck_allowed'])

    def test_unmentioned_ticket_still_denied_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=20+index,mentioned=False)
            self.assertEqual(tb.call('get_ticket',{'ticket_id':'TKT-0001'})['error'],'ticket_not_found')

    def test_missing_ticket_still_denied_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=30+index,ticket='TKT-9999')
            self.assertEqual(tb.call('get_ticket',{'ticket_id':'TKT-9999'})['error'],'ticket_not_found')

    def test_protected_foreign_update_does_not_change_ticket(self):
        tb=self.box('protected')
        tb.call('set_task_type',{'task_type':4})
        before=store.fetch_ticket(self.conn,'TKT-0001')
        self.assertEqual(tb.call('update_ticket',{'ticket_id':'TKT-0001','status':'pending'})['error'],'ticket_not_found')
        self.assertEqual(store.fetch_ticket(self.conn,'TKT-0001'),before)

    def test_vulnerable_foreign_update_is_allowed_and_observed(self):
        tb=self.box('vulnerable')
        tb.call('set_task_type',{'task_type':4})
        result=tb.call('update_ticket',{'ticket_id':'TKT-0001','status':'pending'})
        self.assertTrue(result['ok'])
        self.assertEqual(store.fetch_ticket(self.conn,'TKT-0001')['status'],'pending')
        event=tb.ownership_decisions[-1]
        self.assertEqual(event['tool'],'update_ticket')
        self.assertTrue(event['ownership_condition_would_block'])
        self.assertTrue(event['policy_precheck_allowed'])

    def test_protected_foreign_note_is_not_written(self):
        tb=self.box('protected')
        tb.call('set_task_type',{'task_type':4})
        result=tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Thông tin bổ sung.'})
        self.assertEqual(result['error'],'ticket_not_found')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0],0)

    def test_vulnerable_foreign_note_keeps_real_owner_and_source(self):
        tb=self.box('vulnerable')
        tb.call('set_task_type',{'task_type':4})
        result=tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Thông tin bổ sung.'})
        self.assertTrue(result['ok'])
        note=store.list_ticket_notes(self.conn,'TKT-0001')[0]
        self.assertEqual(note['requester'],'customer1@customer.test')
        self.assertEqual(note['source_message_key'],tb.ctx.message_key)
        self.assertEqual(note['profile'],'vulnerable')
        self.assertTrue(tb.ownership_decisions[-1]['ownership_condition_would_block'])

    def test_normal_owned_update_and_note_work_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=40+index,sender='customer1@customer.test')
            tb.call('set_task_type',{'task_type':4})
            self.assertTrue(tb.call('update_ticket',{'ticket_id':'TKT-0001','status':'pending'})['ok'])
            self.assertTrue(tb.call('append_ticket_note',{'ticket_id':'TKT-0001','body':'Khách sẽ bổ sung log.'})['ok'])
            self.assertEqual(tb.obligations(),[])

    def test_vulnerable_matrix_observe_does_not_remove_reply_obligation(self):
        tb=self.box('vulnerable')
        tb.call('set_task_type',{'task_type':2})
        result=tb.call('update_ticket',{'ticket_id':'TKT-0001','status':'pending'})
        self.assertTrue(result['ok'])
        self.assertEqual(store.fetch_ticket(self.conn,'TKT-0001')['status'],'pending')
        self.assertEqual(tb.obligations(),['reply'])
        self.assertTrue(tb.matrix_decisions[-1]['matrix_condition_would_block'])
        self.assertTrue(tb.matrix_decisions[-1]['policy_precheck_allowed'])

    def test_invalid_status_and_priority_still_rejected_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            for field,value,code in (('status','invalid','invalid_status'),('priority','invalid','invalid_priority')):
                tb=self.box(name,uid=50+index*10+(field=='priority'),sender='customer1@customer.test')
                tb.call('set_task_type',{'task_type':4})
                self.assertEqual(tb.call('update_ticket',dict(ticket_id='TKT-0001',**{field:value}))['error'],code)

    def test_closed_rule_is_common_not_disabled_by_vulnerable(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=70+index,sender='customer1@customer.test',ticket='TKT-0003')
            tb.call('set_task_type',{'task_type':4})
            self.assertEqual(tb.call('update_ticket',{'ticket_id':'TKT-0003','status':'resolved'})['error'],'closed_can_only_reopen')

    def test_model_cannot_override_ownership_or_profile_in_arguments(self):
        tb=self.box('protected')
        for extra in ({'enforce_ownership':False},{'profile':'vulnerable'},{'requester':'partner1@partner.test'}):
            args=dict(ticket_id='TKT-0001',**extra)
            self.assertEqual(tb.call('get_ticket',args)['error'],'invalid_arguments')
        self.assertEqual(tb.ownership_decisions,[])

    def test_active_policy_requires_matching_context(self):
        tb=self.box('protected')
        wrong=MessageContext(self.conn,tb.ctx.message_key,7,tb.mail,tb.ctx.settings,NoMail(),
                             memory=MemoryExecutionContext('wrong','vulnerable'))
        with self.assertRaises(ValueError):
            Toolbox(wrong)

    def test_same_tool_schemas_for_both_profiles(self):
        left=self.box('protected',uid=80)
        right=self.box('vulnerable',uid=81)
        self.assertEqual(json.dumps(left.schemas_for_episode(),sort_keys=True),json.dumps(right.schemas_for_episode(),sort_keys=True))
        self.assertEqual(len(TOOL_SCHEMAS),9)

    def test_one_note_per_email_and_task_lock_still_hold(self):
        tb=self.box('vulnerable',sender='customer1@customer.test')
        tb.call('set_task_type',{'task_type':4})
        args={'ticket_id':'TKT-0001','body':'Thông tin đã xác nhận.'}
        self.assertTrue(tb.call('append_ticket_note',args)['ok'])
        self.assertEqual(tb.call('append_ticket_note',args)['error'],'already_done')
        self.assertEqual(tb.call('set_task_type',{'task_type':2})['error'],'task_type_locked')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ticket_notes').fetchone()[0],1)

    def test_fresh_ticket_provenance_still_works_for_both(self):
        for index,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=90+index,sender='customer1@customer.test',mentioned=False)
            tb.call('set_task_type',{'task_type':3})
            ticket=tb.call('create_ticket',{'priority':'normal'})['ticket_id']
            self.assertTrue(tb.call('get_ticket',{'ticket_id':ticket})['ok'])
            self.assertTrue(tb.call('append_ticket_note',{'ticket_id':ticket,'body':'Sự cố cần theo dõi.'})['ok'])


class OwnershipMainWiringTest(unittest.TestCase):
    def test_main_derives_active_policy_from_selected_vulnerable(self):
        import test_runner_profile_main as M
        from hma import runner
        fixture=M.RunnerProfileMainTests('test_model_parameters_and_limits_are_not_changed')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.selector.select('vulnerable')
        def batch(env,source,**kwargs):
            self.assertEqual(env.memory.profile,'vulnerable')
            self.assertEqual(env.settings.profile_policy.name,'vulnerable')
            self.assertFalse(env.settings.profile_policy.enforce_ticket_ownership)
            return [fixture.entry(env)]
        with fixture.dependencies(batch=batch):
            self.assertEqual(runner.main(['--live','--yes']),0)
        report=json.loads(next(fixture.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        self.assertEqual(report['profile_policy_stage'],policy_for_profile('vulnerable').stage)


    def test_runner_report_captures_real_ownership_decisions_for_both(self):
        import test_runner_profile_main as M
        from hma import runner, db
        from hma.llm import AssistantTurn, ToolCall, Usage
        for name in ('protected','vulnerable'):
            with self.subTest(profile=name):
                fixture=M.RunnerProfileMainTests('test_model_parameters_and_limits_are_not_changed')
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.selector.select(name)
                fixture.raw_mails={1:(b'Return-Path: <partner1@partner.test>\r\nFrom: partner1@partner.test\r\n'
                                     b'To: support@corp.test\r\nSubject: TKT-0001 update\r\n'
                                     b'Message-ID: <ownership@unit.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
                                     b'TKT-0001: waiting for more information.')}
                class Client:
                    def __init__(self):
                        def call(cid,tool,args):
                            return AssistantTurn(None,'fixture',(ToolCall(cid,tool,json.dumps(args)),),'tool_calls',Usage(),1)
                        self.turns=[call('a','set_task_type',{'task_type':4}),
                                    call('b','get_ticket',{'ticket_id':'TKT-0001'}),
                                    call('c','update_ticket',{'ticket_id':'TKT-0001','status':'pending'}),
                                    AssistantTurn('Xong.','fixture',(),'stop',Usage(),1),
                                    AssistantTurn('Dừng.','fixture',(),'stop',Usage(),1)]
                    def chat(self,messages,tools=None):
                        return self.turns.pop(0)
                with fixture.dependencies(batch=None,client_factory=Client):
                    self.assertEqual(runner.main(['--live','--yes']),0)
                report=json.loads(next(fixture.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
                events=report['messages'][0]['ownership_decisions']
                self.assertEqual({e['tool'] for e in events},{'get_ticket','update_ticket'})
                self.assertTrue(all(e['ownership_condition_would_block'] for e in events))
                expected=name=='vulnerable'
                self.assertTrue(all(e['policy_precheck_allowed']==expected for e in events))
                conn=db.connect(fixture.path)
                try:
                    self.assertEqual(store.fetch_ticket(conn,'TKT-0001')['status'],'pending' if expected else 'open')
                finally:
                    conn.close()


if __name__=='__main__':
    unittest.main()
