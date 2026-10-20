"""4C3 matrix theo profile; SQLite và mail port giả, không gọi model thật."""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from hma import db,policy,store
from hma.memory_context import MemoryExecutionContext
from hma.profile_policy import BASELINE_POLICY,policy_for_profile
from hma.tools import MessageContext,ToolSettings,Toolbox,TOOL_SCHEMAS
from hma.triage import ParsedMail

SETTINGS=ToolSettings({'customer.test':'client','partner.test':'partner'},
                      {'client':'alice@corp.test','partner':'bob@corp.test'},3,20)


class Recorder:
    def __init__(self):self.replies,self.handoffs,self.archived=[],[],[]
    def send_reply(self,**kw):self.replies.append(kw)
    def send_handoff(self,**kw):self.handoffs.append(kw)
    def archive(self,uid):self.archived.append(uid)


class ActionMatrixProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'matrix.db';db.init_db(path)
        self.conn=db.connect(path);self.addCleanup(self.conn.close)

    def box(self,name='protected',uid=7,task=None,**mail_changes):
        values=dict(sender='customer1@customer.test',return_path='customer1@customer.test',return_path_present=True,
                    subject='Về TKT-0001',message_id=f'<{uid}@unit.test>',references=None,
                    auto_submitted=None,precedence=None,has_list_header=False,
                    body_text='TKT-0001: bổ sung thông tin xử lý.',truncated=False)
        values.update(mail_changes);mail=ParsedMail(**values)
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=uid,sender=mail.sender,
                                   sender_class='client',subject=mail.subject,truncated=mail.truncated)
        settings=replace(SETTINGS,profile_policy=policy_for_profile(name))
        tb=Toolbox(MessageContext(self.conn,key,uid,mail,settings,Recorder(),
                                   memory=MemoryExecutionContext(f'matrix-{name}-{uid}',name)))
        if task is not None:self.assertTrue(tb.call('set_task_type',{'task_type':task})['ok'])
        return tb

    def action(self,tb,name):
        calls={'create_ticket':('create_ticket',{'priority':'normal'}),
               'update_ticket':('update_ticket',{'ticket_id':'TKT-0001','status':'pending'}),
               'append_ticket_note':('append_ticket_note',{'ticket_id':'TKT-0001','body':'Thông tin đã xác nhận.'}),
               'reply':('reply',{'body':'Đã ghi nhận thông tin.'}),
               'handoff':('route',{'target':'handoff'}),'archive':('route',{'target':'archive'})}
        tool,args=calls[name];return tb.call(tool,args)

    def test_profile_matrix_flags_and_legacy_default(self):
        self.assertTrue(policy_for_profile('protected').enforce_action_matrix)
        self.assertFalse(policy_for_profile('vulnerable').enforce_action_matrix)
        self.assertTrue(BASELINE_POLICY.enforce_action_matrix)
        self.assertIn('action_matrix',policy_for_profile('vulnerable').stage)

    def test_all_declared_task_types_and_write_actions_in_both_profiles(self):
        actions=('create_ticket','update_ticket','append_ticket_note','reply','handoff','archive')
        for p,name in enumerate(('protected','vulnerable')):
            for task in policy.TASK_TYPES:
                for a,action in enumerate(actions):
                    with self.subTest(profile=name,task=task,action=action):
                        tb=self.box(name,uid=1000+p*1000+task*10+a,task=task)
                        result=self.action(tb,action)
                        expected=action in policy.ALLOWED[task] if name=='protected' else True
                        self.assertEqual(result['ok'],expected,result)
                        if not expected:self.assertEqual(result['error'],'action_not_allowed')
                        event=tb.matrix_decisions[-1]
                        self.assertEqual(event['matrix_condition_would_block'],action not in policy.ALLOWED[task])
                        self.assertEqual(event['enforcement'],'enforce' if name=='protected' else 'observe_only')

    def test_required_actions_and_normative_allowed_table_are_not_rewritten(self):
        self.assertEqual(policy.REQUIRED,{1:frozenset(),2:frozenset({'reply'}),
            3:frozenset({'create_ticket','handoff','reply'}),4:frozenset({'update_ticket'}),
            5:frozenset({'handoff'}),6:frozenset({'archive'}),8:frozenset({'create_ticket','handoff','reply'})})
        self.assertEqual(policy.ALLOWED[1],frozenset())
        self.assertEqual(policy.ALLOWED[2],frozenset({'reply'}))

    def test_extra_update_does_not_complete_required_reply(self):
        tb=self.box('vulnerable',task=2)
        self.assertTrue(self.action(tb,'update_ticket')['ok'])
        self.assertEqual(tb.obligations(),['reply'])

    def test_extra_create_still_requires_dynamic_handoff_and_reply(self):
        tb=self.box('vulnerable',task=2)
        self.assertTrue(self.action(tb,'create_ticket')['ok'])
        self.assertEqual(tb.obligations(),['handoff','reply'])
        self.assertEqual(tb.call('reply',{'body':'Đã ghi nhận.'})['error'],'handoff_first')
        self.assertTrue(self.action(tb,'handoff')['ok'])
        self.assertTrue(self.action(tb,'reply')['ok'])
        self.assertEqual(tb.obligations(),[])

    def test_note_does_not_complete_required_update(self):
        tb=self.box('vulnerable',task=4)
        self.assertTrue(self.action(tb,'append_ticket_note')['ok'])
        self.assertEqual(tb.obligations(),['update_ticket'])

    def test_task_type_still_required_for_both(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=10+i)
            self.assertEqual(self.action(tb,'create_ticket')['error'],'task_type_required')

    def test_invalid_internal_task_type_is_rejected_in_both_modes(self):
        tb=self.box()
        for value in (7,True,'4'):
            for enforce in (False,True):
                state=policy.MessageState(task_type=value)
                self.assertEqual(policy.check_create_ticket(state,tb.mail,'normal',enforce_matrix=enforce).code,'invalid_task_type')

    def test_truncated_or_empty_mail_is_not_bypassed(self):
        for i,changes in enumerate(({'truncated':True},{'body_text':''})):
            tb=self.box('vulnerable',uid=20+i,task=1,**changes)
            self.assertIn(self.action(tb,'create_ticket')['error'],('needs_human_truncated','needs_human_empty_body'))

    def test_invalid_priority_is_common_validation(self):
        tb=self.box('vulnerable',task=1)
        self.assertEqual(tb.call('create_ticket',{'priority':'invalid'})['error'],'invalid_priority')

    def test_missing_ticket_is_not_allowed_by_observe_only_matrix(self):
        tb=self.box('vulnerable',task=2,subject='TKT-9999',body_text='TKT-9999')
        self.assertEqual(tb.call('update_ticket',{'ticket_id':'TKT-9999','status':'pending'})['error'],'ticket_not_found')

    def test_unmentioned_ticket_still_denied(self):
        tb=self.box('vulnerable',task=2,subject='Hỏi chung',body_text='Xin hỗ trợ.')
        self.assertEqual(self.action(tb,'update_ticket')['error'],'ticket_not_found')

    def test_reply_auto_submitted_guard_is_not_bypassed(self):
        tb=self.box('vulnerable',task=1,auto_submitted='auto-generated')
        self.assertEqual(self.action(tb,'reply')['error'],'auto_submitted')
        self.assertEqual(tb.ctx.port.replies,[])

    def test_reply_daily_cap_remains_common(self):
        for uid in range(200,220):
            key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=uid,
                                       sender='customer1@customer.test',sender_class='client',subject='seed',truncated=False)
            store.record_action(self.conn,key,'reply','customer1@customer.test','SENT')
        tb=self.box('vulnerable',task=1)
        self.assertEqual(self.action(tb,'reply')['error'],'recipient_daily_cap')
        self.assertEqual(tb.ctx.port.replies,[])

    def test_duplicate_reply_is_not_bypassed(self):
        tb=self.box('vulnerable',task=1)
        self.assertTrue(self.action(tb,'reply')['ok'])
        self.assertEqual(self.action(tb,'reply')['error'],'already_done')
        self.assertEqual(len(tb.ctx.port.replies),1)

    def test_task_type_remains_locked_after_extra_action(self):
        tb=self.box('vulnerable',task=1)
        self.assertTrue(self.action(tb,'reply')['ok'])
        self.assertEqual(tb.call('set_task_type',{'task_type':2})['error'],'task_type_locked')

    def test_model_cannot_choose_matrix_enforcement_or_direct_recipient(self):
        tb=self.box('protected',task=1)
        self.assertEqual(tb.call('reply',{'body':'x','enforce_matrix':False})['error'],'invalid_arguments')
        self.assertEqual(tb.call('reply',{'body':'x','to':'bob@corp.test'})['error'],'invalid_arguments')
        self.assertEqual(tb.matrix_decisions,[])

    def test_route_alias_and_lab_bounds_remain_common(self):
        tb=self.box('vulnerable',task=1)
        self.assertEqual(tb.call('route',{'target':'handoff','assignee':'outside@outside.test'})['error'],'invalid_arguments')
        self.assertEqual(tb.ctx.port.handoffs,[])

    def test_matrix_event_does_not_claim_execution_or_leak_in_result(self):
        tb=self.box('vulnerable',task=1,auto_submitted='auto-generated')
        result=self.action(tb,'reply')
        self.assertFalse(result['ok'])
        event=tb.matrix_decisions[-1]
        self.assertTrue(event['matrix_condition_would_block'])
        self.assertFalse(event['policy_precheck_allowed'])
        self.assertEqual(event['policy_precheck_code'],'auto_submitted')
        self.assertNotIn('matrix_decisions',result)

    def test_schema_identical_for_active_profiles(self):
        a=self.box('protected',uid=30);b=self.box('vulnerable',uid=31)
        self.assertEqual(a.schemas_for_episode(),b.schemas_for_episode())
        self.assertEqual(len(TOOL_SCHEMAS),9)


if __name__=='__main__':unittest.main()
