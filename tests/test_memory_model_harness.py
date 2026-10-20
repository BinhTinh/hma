"""3D1: guard/observer tests offline. Mọi transport đều fake hoặc bị chặn."""
from __future__ import annotations
import contextlib
import io
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import memory_harness as H
from hma.llm import AssistantTurn, LLMError, Usage

SECRET='UNIT_SECRET_DO_NOT_EXPORT'


class NoNetwork:
    def open(self,*args,**kwargs):
        raise AssertionError('Unit test không được gọi mạng')


def turn(tokens=10):
    return AssistantTurn('ok','PRIVATE_REASONING',(),'stop',Usage(1,tokens-1,tokens),1)


class MemoryModelHarnessTests(unittest.TestCase):
    def client(self,**changes):
        options=dict(chat_limit=3,attempt_limit=9,token_threshold=1000,
                     opener=NoNetwork(),sleep=lambda seconds:None)
        options.update(changes)
        return H.ObservedClient('http://unit.invalid',SECRET,'fixture-model',thinking=True,timeout_s=180,**options)

    def test_limits_require_positive_integers(self):
        for name in ('chat_limit','attempt_limit','token_threshold'):
            for value in (0,-1,True,1.5):
                with self.subTest(name=name,value=value):
                    with self.assertRaises(ValueError):
                        self.client(**{name:value})

    def test_episode_required_before_chat(self):
        client=self.client()
        with patch.object(H.LLMClient,'_once') as transport:
            with self.assertRaises(LLMError):
                client.chat([])
            transport.assert_not_called()
        self.assertEqual(client.chat_calls,0)

    def test_chat_limit_stops_before_another_attempt(self):
        client=self.client(chat_limit=1)
        client.begin_episode('one')
        messages=[]
        with patch.object(H.LLMClient,'_once',return_value=turn()) as transport:
            client.chat(messages)
            with self.assertRaises(LLMError):
                client.chat(messages)
            self.assertEqual(transport.call_count,1)
        self.assertEqual((client.chat_calls,len(client.attempts)),(1,1))

    def test_attempt_limit_also_counts_retries(self):
        client=self.client(attempt_limit=2)
        client.begin_episode('one')
        error=LLMError('temporary',retryable=True,status=429)
        with patch.object(H.LLMClient,'_once',side_effect=error) as transport:
            with self.assertRaises(LLMError) as caught:
                client.chat([])
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(transport.call_count,2)
        self.assertEqual(len(client.attempts),2)
        self.assertEqual(client.returned_usage.total_tokens,0)

    def test_retry_then_success_records_attempts_but_usage_once(self):
        client=self.client()
        client.begin_episode('one')
        with patch.object(H.LLMClient,'_once',side_effect=[LLMError('temporary',retryable=True),turn(20)]):
            result=client.chat([])
        self.assertEqual(result.usage.total_tokens,20)
        self.assertEqual(client.returned_usage.total_tokens,20)
        self.assertEqual([a['outcome'] for a in client.attempts],['error','ok'])

    def test_token_threshold_is_post_response_not_a_hard_inflight_cap(self):
        client=self.client(token_threshold=100)
        client.begin_episode('one')
        messages=[]
        with patch.object(H.LLMClient,'_once',return_value=turn(101)) as transport:
            self.assertEqual(client.chat(messages).usage.total_tokens,101)
            with self.assertRaises(LLMError):
                client.chat(messages)
            self.assertEqual(transport.call_count,1)
        self.assertEqual(client.returned_usage.total_tokens,101)

    def test_payload_keeps_model_thinking_and_original_output_defaults(self):
        client=self.client()
        payload=json.loads(client._payload([{'role':'user','content':'hello'}],None))
        self.assertEqual(payload['model'],'fixture-model')
        self.assertEqual(payload['thinking'],{'type':'enabled'})
        self.assertNotIn('max_tokens',payload)
        self.assertNotIn('reasoning_effort',payload)
        self.assertNotIn(SECRET,json.dumps(payload))
        self.assertEqual(client.max_retries,2)

    def test_histories_are_observed_not_injected_or_mutated(self):
        client=self.client()
        first=[{'role':'user','content':'first'}]
        second=[{'role':'user','content':'second'}]
        with patch.object(H.LLMClient,'_once',return_value=turn()):
            client.begin_episode('one')
            client.chat(first)
            client.begin_episode('two')
            client.chat(second)
        self.assertEqual(first,[{'role':'user','content':'first'}])
        self.assertEqual(second,[{'role':'user','content':'second'}])
        self.assertIs(client.histories['one'],first)
        self.assertIs(client.histories['two'],second)

    def test_reasoning_is_kept_for_runtime_but_removed_from_export(self):
        client=self.client()
        messages=[]
        client.begin_episode('one')
        with patch.object(H.LLMClient,'_once',return_value=turn()):
            response=client.chat(messages)
        messages.append(response.to_message())
        exported=client.export_traces()
        self.assertIn('reasoning_content',messages[0])
        self.assertNotIn('reasoning_content',exported['one']['messages'][0])
        self.assertNotIn('PRIVATE_REASONING',json.dumps(exported))

    def test_recursive_scrub_does_not_mutate_original(self):
        original={'nested':[SECRET,{'value':'prefix '+SECRET}]}
        result=H.scrub(original,SECRET)
        self.assertNotIn(SECRET,json.dumps(result))
        self.assertEqual(original['nested'][0],SECRET)

    def test_console_formatter_masks_secret(self):
        record=logging.LogRecord('unit',logging.WARNING,'x',1,'failure '+SECRET,(),None)
        self.assertNotIn(SECRET,H.SecretFormatter(SECRET).format(record))

    def test_changed_plan_is_refused(self):
        current={'ready_for_harness_draft':True,'issues':[],'model':'a'}
        approved=dict(current,model='b')
        with self.assertRaises(ValueError):
            H.validate_plan(current,approved)

    def test_unready_plan_is_refused_even_if_equal(self):
        current={'ready_for_harness_draft':False,'issues':['missing']}
        with self.assertRaises(ValueError):
            H.validate_plan(current,current)

    def test_default_mode_does_not_execute_or_construct_client(self):
        cfg=SimpleNamespace(llm=SimpleNamespace(api_key=SECRET))
        plan={'ready_for_harness_draft':True,'issues':[]}
        with patch.object(H,'prepare',return_value=(cfg,[],plan)), patch.object(H,'execute') as execute,              patch.object(H,'ObservedClient') as client, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(H.main(['--plan','unused.json']),0)
            execute.assert_not_called()
            client.assert_not_called()

    def test_execute_requires_explicit_api_confirmation(self):
        with patch.object(H,'prepare') as prepare, patch.object(H,'execute') as execute,              contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                H.main(['--plan','unused.json','--execute'])
            self.assertEqual(caught.exception.code,2)
            prepare.assert_not_called()
            execute.assert_not_called()

    def test_confirmed_mode_delegates_once_without_network_in_unit_test(self):
        cfg=SimpleNamespace(llm=SimpleNamespace(api_key=SECRET))
        with patch.object(H,'prepare',return_value=(cfg,[],{})), patch.object(H,'execute',return_value=0) as execute,              patch.object(H.logging,'basicConfig'):
            self.assertEqual(H.main(['--plan','unused.json','--execute','--confirm-api-usage']),0)
            execute.assert_called_once()

    def test_json_writer_refuses_to_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json'
            H.write_json(path,{'value':SECRET},SECRET)
            self.assertNotIn(SECRET,path.read_text())
            before=path.read_bytes()
            with self.assertRaises(FileExistsError):
                H.write_json(path,{},SECRET)
            self.assertEqual(path.read_bytes(),before)

    def test_event_sink_masks_secret_and_writes_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.jsonl'
            sink=H.EventSink(path,SECRET)
            sink({'event':'first','value':SECRET})
            sink({'event':'second'})
            text=path.read_text()
            self.assertNotIn(SECRET,text)
            self.assertEqual(len([json.loads(line) for line in text.splitlines()]),2)

    def test_structural_success_still_needs_review(self):
        entries=[{'status':'DONE','tickets':[{'id':'TKT-0004'}], 'ticket_notes_written':[{'id':1}],
                  'handoffs':[{'to':'alice@corp.test'}],'replies':[{'to':'customer1@customer.test'}]},
                 {'status':'DONE','ticket_notes_read':[{'note_ids':[1]}],
                  'replies':[{'to':'customer1@customer.test'}]}]
        checks=H.structural_checks(entries,{'status':'pending'},'customer1@customer.test','alice@corp.test')
        self.assertTrue(all(checks.values()))
        self.assertEqual(H.checks_status(checks),'completed_needs_review')

    def test_missing_second_episode_is_not_success(self):
        checks=H.structural_checks([],None,'customer1@customer.test','alice@corp.test')
        self.assertFalse(checks['second_ran'])
        self.assertEqual(H.checks_status(checks),'scenario_incomplete')
        self.assertEqual(H.checks_status({}),'scenario_incomplete')

    def test_duplicate_or_third_episode_is_refused(self):
        client=self.client()
        client.begin_episode('one')
        with self.assertRaises(ValueError):
            client.begin_episode('one')
        client.begin_episode('two')
        with self.assertRaises(LLMError):
            client.begin_episode('three')


if __name__=='__main__':
    unittest.main()
