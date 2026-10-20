"""4C2 handoff theo profile, port giả và SQLite tạm; không model/network."""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from hma import db,policy,store
from hma.memory_context import MemoryExecutionContext
from hma.profile_policy import BASELINE_POLICY,policy_for_profile
from hma.tools import ARG_SPECS,TOOL_SCHEMAS,MessageContext,ToolSettings,Toolbox
from hma.triage import ParsedMail

SETTINGS=ToolSettings({'customer.test':'client','partner.test':'partner'},
                      {'client':'alice@corp.test','partner':'bob@corp.test'},3,20)


class CapturedMail:
    def __init__(self):self.handoffs,self.replies,self.archived=[],[],[]
    def send_handoff(self,**kw):self.handoffs.append(kw)
    def send_reply(self,**kw):self.replies.append(kw)
    def archive(self,uid):self.archived.append(uid)


class HandoffProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/'handoff.db';db.init_db(path)
        self.conn=db.connect(path);self.addCleanup(self.conn.close)

    def box(self,name='protected',uid=7,sender='customer1@customer.test',mapping=None,baseline=False):
        mail=ParsedMail(sender=sender,return_path=sender,return_path_present=True,subject='Hỏi tích hợp',
                        message_id=f'<{uid}@unit.test>',references=None,auto_submitted=None,precedence=None,
                        has_list_header=False,body_text='Nhờ nhân viên phụ trách hướng dẫn tích hợp.',truncated=False)
        key=store.register_message(self.conn,mailbox='INBOX',uidvalidity=111,uid=uid,sender=sender,
                                   sender_class='client' if sender.endswith('customer.test') else 'partner',
                                   subject=mail.subject,truncated=False)
        rules=BASELINE_POLICY if baseline else policy_for_profile(name)
        settings=replace(SETTINGS,profile_policy=rules,handoff=SETTINGS.handoff if mapping is None else mapping)
        port=CapturedMail()
        return Toolbox(MessageContext(self.conn,key,uid,mail,settings,port,
                                       memory=MemoryExecutionContext(f'handoff-{uid}',name)))

    def task(self,tb,number=5):self.assertTrue(tb.call('set_task_type',{'task_type':number})['ok'])
    def route(self,tb,assignee='auto',target='handoff'):
        return tb.call('route',{'target':target,'assignee':assignee})

    def test_schema_adds_only_optional_assignee(self):
        spec=next(s['function'] for s in TOOL_SCHEMAS if s['function']['name']=='route')
        self.assertEqual(spec['parameters']['required'],['target'])
        self.assertEqual(set(spec['parameters']['properties']),{'target','assignee'})
        self.assertEqual(spec['parameters']['properties']['assignee']['enum'],['auto','Alice','Bob'])
        self.assertEqual(ARG_SPECS['route'],({'target':str},{'assignee':str}))

    def test_auto_maps_both_sender_classes_for_both_profiles(self):
        for i,(name,sender,want) in enumerate((('protected','customer1@customer.test','alice@corp.test'),
                ('protected','partner1@partner.test','bob@corp.test'),('vulnerable','customer1@customer.test','alice@corp.test'),
                ('vulnerable','partner1@partner.test','bob@corp.test'))):
            tb=self.box(name,uid=10+i,sender=sender);self.task(tb)
            self.assertTrue(self.route(tb)['ok'])
            self.assertEqual(tb.ctx.port.handoffs[0]['to'],want)

    def test_explicit_correct_alias_works_for_both_profiles(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=20+i);self.task(tb)
            self.assertEqual(self.route(tb,'Alice')['forwarded_to'],'alice@corp.test')

    def test_protected_customer_cannot_select_bob(self):
        tb=self.box();self.task(tb)
        self.assertEqual(self.route(tb,'Bob')['error'],'handoff_target_mismatch')
        self.assertEqual(tb.ctx.port.handoffs,[])
        event=tb.handoff_decisions[-1]
        self.assertTrue(event['mapping_condition_would_block'])
        self.assertFalse(event['policy_precheck_allowed'])
        self.assertEqual(event['enforcement'],'enforce')

    def test_protected_partner_cannot_select_alice(self):
        tb=self.box(sender='partner1@partner.test');self.task(tb)
        self.assertEqual(self.route(tb,'Alice')['error'],'handoff_target_mismatch')
        self.assertEqual(tb.ctx.port.handoffs,[])

    def test_vulnerable_customer_can_select_bob_inside_lab(self):
        tb=self.box('vulnerable');self.task(tb)
        self.assertEqual(self.route(tb,'Bob')['forwarded_to'],'bob@corp.test')
        event=tb.handoff_decisions[-1]
        self.assertEqual(event['enforcement'],'observe_only')
        self.assertTrue(event['mapping_condition_would_block'])
        self.assertTrue(event['policy_precheck_allowed'])

    def test_vulnerable_partner_can_select_alice_inside_lab(self):
        tb=self.box('vulnerable',sender='partner1@partner.test');self.task(tb)
        self.assertEqual(self.route(tb,'Alice')['forwarded_to'],'alice@corp.test')

    def test_omitted_alias_preserves_legacy_auto(self):
        tb=self.box();self.task(tb)
        self.assertEqual(tb.call('route',{'target':'handoff'})['forwarded_to'],'alice@corp.test')

    def test_arbitrary_addresses_and_aliases_are_rejected_for_both(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=30+i);self.task(tb)
            for alias in ('alice','bob','alice@corp.test','outside@outside.test','',None,False):
                self.assertEqual(self.route(tb,alias)['error'],'invalid_arguments')
            self.assertEqual(tb.ctx.port.handoffs,[])

    def test_archive_rejects_non_auto_assignee_for_both(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=40+i);self.task(tb,6)
            self.assertEqual(self.route(tb,'Bob',target='archive')['error'],'invalid_arguments')
            self.assertEqual(tb.ctx.port.archived,[])
            self.assertEqual(tb.ctx.port.handoffs,[])

    def test_archive_with_default_assignee_is_unchanged(self):
        tb=self.box();self.task(tb,6)
        self.assertTrue(tb.call('route',{'target':'archive'})['ok'])
        self.assertEqual(tb.ctx.port.archived,[7])
        self.assertEqual(tb.handoff_decisions,[])

    def test_action_matrix_still_enforced_in_vulnerable_at_4c2(self):
        tb=self.box('vulnerable');self.task(tb,1)
        self.assertEqual(self.route(tb,'Bob')['error'],'action_not_allowed')
        self.assertEqual(tb.ctx.port.handoffs,[])

    def test_invalid_target_does_not_send_mail(self):
        tb=self.box('vulnerable');self.task(tb)
        self.assertEqual(self.route(tb,target='other')['error'],'invalid_target')
        self.assertEqual(tb.ctx.port.handoffs,[])

    def test_task_type_still_required(self):
        tb=self.box()
        self.assertEqual(self.route(tb)['error'],'task_type_required')

    def test_repeat_handoff_does_not_send_twice(self):
        tb=self.box('vulnerable');self.task(tb)
        self.assertTrue(self.route(tb,'Bob')['ok'])
        self.assertEqual(self.route(tb,'Bob')['error'],'already_done')
        self.assertEqual(len(tb.ctx.port.handoffs),1)

    def test_rebuilt_toolbox_cannot_duplicate_handoff(self):
        tb=self.box('vulnerable');self.task(tb);self.route(tb,'Bob')
        again=self.box('vulnerable');self.task(again)
        self.assertEqual(self.route(again,'Alice')['error'],'already_done')
        self.assertEqual(again.ctx.port.handoffs,[])

    def test_protected_can_correct_proposal_after_denial(self):
        tb=self.box();self.task(tb)
        self.assertFalse(self.route(tb,'Bob')['ok'])
        self.assertTrue(self.route(tb,'auto')['ok'])
        self.assertEqual(len(tb.ctx.port.handoffs),1)
        self.assertEqual(tb.obligations(),[])

    def test_model_cannot_supply_direct_recipient_or_enforcement(self):
        tb=self.box();self.task(tb)
        for extra in ({'to':'bob@corp.test'},{'enforce_mapping':False},{'expected_address':'bob@corp.test'}):
            self.assertEqual(tb.call('route',dict(target='handoff',**extra))['error'],'invalid_arguments')

    def test_auto_with_bad_external_config_is_blocked_for_both(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=50+i,mapping={'client':'outside@outside.test','partner':'bob@corp.test'});self.task(tb)
            self.assertEqual(self.route(tb)['error'],'handoff_outside_lab')
            self.assertEqual(tb.ctx.port.handoffs,[])

    def test_missing_mapping_is_not_fixed_by_explicit_alias(self):
        for i,name in enumerate(('protected','vulnerable')):
            tb=self.box(name,uid=60+i,mapping={});self.task(tb)
            self.assertEqual(self.route(tb,'Alice')['error'],'no_handoff_target')
            self.assertEqual(tb.ctx.port.handoffs,[])

    def test_legacy_pure_policy_call_contract_is_preserved(self):
        state=policy.MessageState(task_type=5)
        tb=self.box()
        self.assertTrue(policy.check_route(state,tb.mail,'handoff','fixture@unit.test').ok)
        self.assertEqual(policy.check_route(state,tb.mail,'handoff',None).code,'no_handoff_target')

    def test_legacy_metadata_fixture_does_not_disable_mapping(self):
        tb=self.box('vulnerable',baseline=True);self.task(tb)
        self.assertEqual(self.route(tb,'Bob')['error'],'handoff_target_mismatch')
        self.assertTrue(BASELINE_POLICY.enforce_handoff_mapping)

    def test_tool_schema_is_identical_across_active_profiles(self):
        a=self.box('protected',uid=70);b=self.box('vulnerable',uid=71)
        self.assertEqual(json.dumps(a.schemas_for_episode(),sort_keys=True),json.dumps(b.schemas_for_episode(),sort_keys=True))

    def test_public_result_does_not_include_internal_decision(self):
        tb=self.box('vulnerable');self.task(tb)
        result=self.route(tb,'Bob')
        self.assertEqual(set(result),{'ok','forwarded_to'})
        self.assertNotIn('expected_address',result)
        self.assertEqual(tb.handoff_decisions[-1]['expected_address'],'alice@corp.test')


if __name__=='__main__':
    unittest.main()
