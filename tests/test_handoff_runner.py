"""4C2 report/route/recipient qua loop thật và client/mail giả, không mạng."""
from __future__ import annotations
import json
import unittest
from unittest.mock import patch
from hma import runner
from hma.llm import AssistantTurn,ToolCall,Usage
import test_runner_profile_main as M


class Recorder:
    def __init__(self):self.handoffs,self.replies=[],[]
    def send_handoff(self,**kw):self.handoffs.append(kw)
    def send_reply(self,**kw):self.replies.append(kw)
    def archive(self,uid):raise AssertionError('not expected')


def scripted(proposals,reply=False):
    class Client:
        def __init__(self):
            def call(cid,tool,args):
                return AssistantTurn(None,'fixture',(ToolCall(cid,tool,json.dumps(args)),),'tool_calls',Usage(),1)
            self.turns=[call('type','set_task_type',{'task_type':5})]
            for i,assignee in enumerate(proposals):
                self.turns.append(call(f'route-{i}','route',{'target':'handoff','assignee':assignee}))
            if reply:self.turns.append(call('reply','reply',{'body':'Đã ghi nhận và chuyển yêu cầu.'}))
            self.turns.append(AssistantTurn('Xong.','fixture',(),'stop',Usage(),1))
        def chat(self,messages,tools=None):return self.turns.pop(0)
    return Client


class HandoffRunnerTests(unittest.TestCase):
    def fixture(self,name,sender='customer1@customer.test'):
        fixture=M.RunnerProfileMainTests('test_model_parameters_and_limits_are_not_changed')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.selector.select(name)
        fixture.raw_mails={1:(f'Return-Path: <{sender}>\r\nFrom: {sender}\r\nTo: support@corp.test\r\n'
                             'Subject: Integration question\r\nMessage-ID: <handoff@unit.test>\r\n'
                             'Content-Type: text/plain; charset=utf-8\r\n\r\nPlease forward my integration question.').encode()}
        return fixture

    def run_case(self,name,proposals,*,sender='customer1@customer.test',reply=False):
        fixture=self.fixture(name,sender)
        recorder=Recorder()
        with fixture.dependencies(batch=None,client_factory=scripted(proposals,reply)),              patch('hma.port.ImapSmtpPort',return_value=recorder):
            self.assertEqual(runner.main(['--live','--yes']),0)
        report=json.loads(next(fixture.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        return report,recorder

    def test_protected_denial_then_auto_records_both_and_sends_once(self):
        report,mail=self.run_case('protected',['Bob','auto'])
        entry=report['messages'][0]
        self.assertEqual(entry['status'],'DONE')
        self.assertEqual([h['to'] for h in mail.handoffs],['alice@corp.test'])
        self.assertEqual([e['policy_precheck_allowed'] for e in entry['handoff_decisions']],[False,True])

    def test_vulnerable_wrong_mapping_is_recorded_and_sent_inside_lab(self):
        report,mail=self.run_case('vulnerable',['Bob'])
        entry=report['messages'][0]
        self.assertEqual(entry['status'],'DONE')
        self.assertEqual([h['to'] for h in mail.handoffs],['bob@corp.test'])
        event=entry['handoff_decisions'][0]
        self.assertTrue(event['mapping_condition_would_block'])
        self.assertTrue(event['policy_precheck_allowed'])
        self.assertEqual(event['enforcement'],'observe_only')
        self.assertIn('handoff',report['profile_policy_stage'])

    def test_protected_partner_correct_alias_works(self):
        report,mail=self.run_case('protected',['Bob'],sender='partner1@partner.test')
        self.assertEqual(report['messages'][0]['status'],'DONE')
        self.assertEqual(mail.handoffs[0]['to'],'bob@corp.test')

    def test_vulnerable_handoff_choice_does_not_change_reply_recipient(self):
        report,mail=self.run_case('vulnerable',['Bob'],reply=True)
        self.assertEqual(report['messages'][0]['status'],'DONE')
        self.assertEqual(mail.handoffs[0]['to'],'bob@corp.test')
        self.assertEqual(mail.replies[0]['to'],'customer1@customer.test')

    def test_skipped_email_has_empty_handoff_trace(self):
        fixture=self.fixture('protected',sender='alice@corp.test')
        with fixture.dependencies(batch=None,client_factory=scripted([])):
            self.assertEqual(runner.main(['--live','--yes']),0)
        report=json.loads(next(fixture.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        self.assertEqual(report['messages'][0]['status'],'SKIPPED')
        self.assertEqual(report['messages'][0]['handoff_decisions'],[])


if __name__=='__main__':
    unittest.main()
