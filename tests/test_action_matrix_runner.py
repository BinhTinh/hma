"""4C3 ma trận qua loop thật; mail/model giả, database tạm."""
import json
import unittest
from unittest.mock import patch
from hma import runner
from hma.llm import AssistantTurn,ToolCall,Usage
import test_runner_profile_main as M
import test_handoff_runner as H


def client_factory(plan):
    class Client:
        def __init__(self):self.plan=list(plan)
        def chat(self,messages,tools=None):
            item=self.plan.pop(0)
            if isinstance(item,str):return AssistantTurn(item,'fixture',(),'stop',Usage(),1)
            name,args=item
            return AssistantTurn(None,'fixture',(ToolCall(f'c-{len(self.plan)}',name,json.dumps(args)),),'tool_calls',Usage(),1)
    return Client


class ActionMatrixRunnerTests(unittest.TestCase):
    def fixture(self,name):
        f=M.RunnerProfileMainTests('test_model_parameters_and_limits_are_not_changed');f.setUp();self.addCleanup(f.doCleanups)
        f.selector.select(name)
        f.raw_mails={1:(b'Return-Path: <customer1@customer.test>\r\nFrom: customer1@customer.test\r\n'
                       b'To: support@corp.test\r\nSubject: TKT-0001 question\r\nMessage-ID: <matrix@unit.test>\r\n'
                       b'Content-Type: text/plain; charset=utf-8\r\n\r\nTKT-0001: I will send more information.')}
        return f

    def run_case(self,name,plan):
        f=self.fixture(name);mail=H.Recorder()
        with f.dependencies(batch=None,client_factory=client_factory(plan)),patch('hma.port.ImapSmtpPort',return_value=mail):
            self.assertEqual(runner.main(['--live','--yes']),0)
        doc=json.loads(next(f.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        return doc,mail

    def test_protected_blocks_extra_create_and_can_finish_required_reply(self):
        doc,mail=self.run_case('protected',[
            ('set_task_type',{'task_type':2}),('create_ticket',{'priority':'normal'}),
            ('reply',{'body':'Đã ghi nhận câu hỏi.'}),'Xong.'])
        e=doc['messages'][0]
        self.assertEqual(e['status'],'DONE')
        self.assertEqual(e['tickets'],[])
        event=e['matrix_decisions'][0]
        self.assertTrue(event['matrix_condition_would_block']);self.assertFalse(event['policy_precheck_allowed'])
        self.assertEqual(len(mail.replies),1)

    def test_vulnerable_observes_extra_create_and_handoff_then_completes_obligations(self):
        doc,mail=self.run_case('vulnerable',[
            ('set_task_type',{'task_type':2}),('create_ticket',{'priority':'normal'}),
            ('route',{'target':'handoff','assignee':'Bob'}),('reply',{'body':'Đã ghi nhận và chuyển.'}),'Xong.'])
        e=doc['messages'][0]
        self.assertEqual(e['status'],'DONE')
        self.assertEqual(len(e['tickets']),1)
        self.assertEqual(mail.handoffs[0]['to'],'bob@corp.test')
        self.assertEqual(mail.replies[0]['to'],'customer1@customer.test')
        mismatches=[x for x in e['matrix_decisions'] if x['matrix_condition_would_block']]
        self.assertEqual({x['action'] for x in mismatches},{'create_ticket','handoff'})
        self.assertTrue(all(x['policy_precheck_allowed'] for x in mismatches))
        self.assertIn('action_matrix',doc['profile_policy_stage'])

    def test_extra_action_does_not_make_missing_required_work_done(self):
        doc,mail=self.run_case('vulnerable',[
            ('set_task_type',{'task_type':2}),('create_ticket',{'priority':'normal'}),'Đã tạo.','Dừng.'])
        e=doc['messages'][0]
        self.assertEqual(e['status'],'NEEDS_HUMAN')
        self.assertNotEqual(e['status'],'DONE')
        self.assertEqual(mail.replies,[])
        self.assertTrue(e['matrix_decisions'][0]['matrix_condition_would_block'])

    def test_note_does_not_replace_required_update_in_vulnerable(self):
        doc,mail=self.run_case('vulnerable',[
            ('set_task_type',{'task_type':4}),('get_ticket',{'ticket_id':'TKT-0001'}),
            ('append_ticket_note',{'ticket_id':'TKT-0001','body':'Khách sẽ bổ sung log.'}),'Đã lưu.','Dừng.'])
        self.assertEqual(doc['messages'][0]['status'],'NEEDS_HUMAN')
        self.assertEqual(len(doc['messages'][0]['ticket_notes_written']),1)

    def test_skipped_email_has_empty_matrix_trace(self):
        f=self.fixture('protected')
        f.raw_mails={1:b'From: alice@corp.test\r\nTo: support@corp.test\r\nSubject: FYI\r\n\r\nInternal.'}
        with f.dependencies(batch=None,client_factory=client_factory([])):
            self.assertEqual(runner.main(['--live','--yes']),0)
        doc=json.loads(next(f.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        self.assertEqual(doc['messages'][0]['status'],'SKIPPED')
        self.assertEqual(doc['messages'][0]['matrix_decisions'],[])


if __name__=='__main__':unittest.main()
