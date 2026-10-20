"""4B main lifecycle: mocks cho config/mail/model, SQLite temp; không mạng."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from hma import db, kb, health, runner
from hma.kb import KBDoc
from hma.llm import AssistantTurn, ToolCall, Usage
from hma.mailio import MailError
from hma.profiles import ProfileSelector
from hma.runtime_binding import context_metadata
from hma.runtime_lock import RunBusy


class NoNetworkClient:
    def chat(self,*args,**kwargs):
        raise AssertionError('main wiring test không được gọi model thật')


class NoNetworkMail:
    def send_reply(self,**kwargs):
        raise AssertionError('không gửi SMTP thật')
    def send_handoff(self,**kwargs):
        raise AssertionError('không handoff thật')
    def archive(self,uid):
        raise AssertionError('không đổi mailbox thật')


class FakeSession:
    def __init__(self,mails,on_exit=None):
        self.mails,self.on_exit=mails,on_exit
        self.fetched=[]
        self.readonly=None
    def __enter__(self):
        return self
    def __exit__(self,*exc):
        if self.on_exit:
            self.on_exit()
    def select(self,name,readonly=False):
        self.readonly=readonly
        return SimpleNamespace(uidvalidity=111,exists=len(self.mails))
    def capabilities(self):
        return frozenset({'MOVE'})
    def search_uids(self,query):
        return list(self.mails)
    def fetch_raw(self,uid):
        self.fetched.append(uid)
        return self.mails[uid]


class ScriptedClient:
    def __init__(self):
        self.calls=0
        self.script=[AssistantTurn(None,'fixture',(ToolCall('a','set_task_type','{"task_type":1}'),),
                                   'tool_calls',Usage(),1),
                     AssistantTurn('Xong.','fixture',(),'stop',Usage(),1)]
    def chat(self,messages,tools=None):
        self.calls+=1
        if not self.script:
            raise AssertionError('Không được gọi lại email terminal')
        return self.script.pop(0)


class RunnerProfileMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'data'/'agent.db'
        db.init_db(self.path)
        conn=db.connect(self.path)
        try:
            self.docs=[KBDoc('KB-018','Mức ưu tiên','Ảnh hưởng một người: normal.')]
            kb.replace_kb(conn,self.docs,('KB-018',))
        finally:
            conn.close()
        self.selector=ProfileSelector(self.path)
        self.cfg=SimpleNamespace(
            paths=SimpleNamespace(db=self.path,kb_dir=self.root/'kb',log_dir=self.root/'logs'),
            llm=SimpleNamespace(base_url='http://unit.invalid',api_key='UNIT_ONLY',model='fixture-model',thinking=True,request_timeout_s=180.0),
            mail=SimpleNamespace(host='mail.invalid',imap_port=993,smtp_port=587,user='support@corp.test',ca_file=self.root/'ca.crt',password_env='UNIT_PASS'),
            agent=SimpleNamespace(max_steps=8,episode_timeout_s=120,episode_token_cap=60000,body_limit_chars=6000,
                                  kb_top_k=3,max_run_messages=100,max_replies_per_recipient_per_day=20),
            sender_classes={'customer.test':'client','partner.test':'partner'},
            handoff={'client':'alice@corp.test','partner':'bob@corp.test'},kb_always_load=('KB-018',))
        self.clients,self.sessions=[],[]
        self.session_exit=None
        self.raw_mails={}

    @contextlib.contextmanager
    def dependencies(self,batch=lambda env,source,**kwargs:[],client_factory=None,session_failure=None):
        def create_client(*args,**kwargs):
            client=client_factory() if client_factory else NoNetworkClient()
            self.clients.append(client)
            return client
        def create_session(*args,**kwargs):
            if session_failure:
                raise session_failure
            session=FakeSession(self.raw_mails,self.session_exit)
            self.sessions.append(session)
            return session
        with contextlib.ExitStack() as stack:
            config=stack.enter_context(patch('hma.config.load_config',return_value=self.cfg))
            password=stack.enter_context(patch('hma.a0.get_password',return_value='fixture-password'))
            tls=stack.enter_context(patch('hma.mailio.make_ssl_context',return_value=object()))
            llm=stack.enter_context(patch('hma.llm.LLMClient',side_effect=create_client))
            imap=stack.enter_context(patch('hma.mailio.ImapSession',side_effect=create_session))
            smtp=stack.enter_context(patch('hma.mailio.SmtpSender',return_value=object()))
            stack.enter_context(patch('hma.port.ImapSmtpPort',return_value=NoNetworkMail()))
            stack.enter_context(patch('hma.kb.load_kb_dir',return_value=self.docs))
            if batch is not None:
                stack.enter_context(patch.object(runner,'run_batch',side_effect=batch))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield SimpleNamespace(config=config,password=password,tls=tls,llm=llm,imap=imap,smtp=smtp)

    def entry(self,env,uid=1):
        return {'uid':uid,'sender':'customer1@customer.test','subject':'fixture','status':'DONE','reason':'',
                'tokens':{'total':0},**context_metadata(env.memory, profile_policy=env.settings.profile_policy)}

    def assert_locked(self):
        with self.assertRaises(RunBusy):
            self.selector.select('vulnerable')

    def test_missing_choice_in_both_modes_stops_before_credentials_or_clients(self):
        for args in ([],['--live','--yes']):
            with self.subTest(args=args):
                with self.dependencies() as mocks:
                    with self.assertRaises(runner.RunnerError):
                        runner.main(args)
                    for service in (mocks.password,mocks.tls,mocks.llm,mocks.imap,mocks.smtp):
                        service.assert_not_called()
        self.assertFalse(self.selector.state_path.exists())

    def test_corrupt_choice_stops_before_io(self):
        self.selector.state_path.write_text('{bad')
        with self.dependencies() as mocks:
            with self.assertRaises(runner.RunnerError):
                runner.main(['--live','--yes'])
            mocks.password.assert_not_called()
            mocks.llm.assert_not_called()
            mocks.imap.assert_not_called()

    def test_if_idle_busy_is_quiet_in_both_modes_without_reading_choice(self):
        with runner.RunLock(self.selector.lock_path):
            for args in (['--if-idle'],['--live','--yes','--if-idle']):
                with self.dependencies() as mocks:
                    self.assertEqual(runner.main(args),0)
                    mocks.password.assert_not_called()
                    mocks.llm.assert_not_called()
                    mocks.imap.assert_not_called()
        self.assertFalse(self.selector.state_path.exists())

    def test_busy_without_if_idle_remains_an_error(self):
        with runner.RunLock(self.selector.lock_path):
            with self.dependencies() as mocks:
                with self.assertRaises(runner.RunnerError):
                    runner.main(['--live','--yes'])
                mocks.password.assert_not_called()

    def test_lock_covers_batch_finish_report_and_session_cleanup(self):
        self.selector.select('protected')
        seen=[]
        def batch(env,source,**kwargs):
            self.assert_locked()
            seen.append(env.memory)
            kwargs['heartbeat']([])
            entries=[self.entry(env)]
            kwargs['heartbeat'](entries)
            return entries
        original=runner.finish_pass
        def finish(*args,**kwargs):
            self.assert_locked()
            self.assertIs(kwargs['memory'],seen[0])
            return original(*args,**kwargs)
        def on_exit():
            self.assert_locked()
            self.assertEqual(health.read_heartbeat(self.path.parent/'heartbeat.json')['state'],'idle')
            self.assertEqual(len(list(self.cfg.paths.log_dir.glob('run-live-*.json'))),1)
        self.session_exit=on_exit
        with self.dependencies(batch=batch),patch.object(runner,'finish_pass',side_effect=finish):
            self.assertEqual(runner.main(['--live','--yes']),0)
        self.selector.select('vulnerable')

    def test_dry_run_also_holds_lock_and_binds_choice_without_smtp_or_heartbeat(self):
        self.selector.select('protected')
        seen=[]
        def batch(env,source,**kwargs):
            self.assert_locked()
            seen.append(env.memory)
            self.assertIsNone(kwargs['heartbeat'])
            return [self.entry(env)]
        with self.dependencies(batch=batch) as mocks:
            self.assertEqual(runner.main([]),0)
            mocks.smtp.assert_not_called()
        self.assertEqual(seen[0].profile,'protected')
        self.assertTrue(self.sessions[0].readonly)
        self.assertFalse((self.path.parent/'heartbeat.json').exists())

    def test_report_heartbeat_and_two_entries_use_one_context(self):
        self.selector.select('protected')
        observed=[]
        def batch(env,source,**kwargs):
            observed.append(env.memory)
            entries=[self.entry(env,1),self.entry(env,2)]
            kwargs['heartbeat'](entries)
            running=health.read_heartbeat(self.path.parent/'heartbeat.json')
            self.assertEqual(running['run_id'],env.memory.run_id)
            self.assertEqual(running['state'],'running')
            return entries
        with self.dependencies(batch=batch):
            self.assertEqual(runner.main(['--live','--yes']),0)
        report=json.loads(next(self.cfg.paths.log_dir.glob('run-live-*.json')).read_text())
        beat=health.read_heartbeat(self.path.parent/'heartbeat.json')
        from hma.profile_policy import policy_for_profile
        self.assertEqual(report['profile_policy_stage'],policy_for_profile('protected').stage)
        for entry in report['messages']:
            self.assertEqual((entry['run_id'],entry['profile']),(report['run_id'],report['profile']))
        self.assertEqual((beat['run_id'],beat['profile']),(report['run_id'],report['profile']))
        self.assertEqual(report['run_id'],observed[0].run_id)

    def test_empty_batch_has_context_heartbeat_but_no_report(self):
        self.selector.select('protected')
        with self.dependencies():
            self.assertEqual(runner.main(['--live','--yes']),0)
        self.assertFalse(self.cfg.paths.log_dir.exists())
        beat=health.read_heartbeat(self.path.parent/'heartbeat.json')
        self.assertEqual((beat['state'],beat['processed'],beat['profile']),('idle',0,'protected'))
        self.assertTrue(beat['run_id'].startswith('hma-run-'))

    def test_report_names_are_unique_even_with_same_timestamp(self):
        self.selector.select('protected')
        def batch(env,source,**kwargs):
            return [self.entry(env)]
        with self.dependencies(batch=batch),patch.object(runner.time,'strftime',return_value='FIXED'):
            self.assertEqual(runner.main(['--live','--yes']),0)
            self.assertEqual(runner.main(['--live','--yes']),0)
        reports=list(self.cfg.paths.log_dir.glob('run-live-*.json'))
        self.assertEqual(len(reports),2)
        self.assertEqual(len({json.loads(p.read_text())['run_id'] for p in reports}),2)

    def test_main_reads_selector_once_per_admitted_run(self):
        self.selector.select('protected')
        original=ProfileSelector.read
        with patch.object(ProfileSelector,'read',autospec=True,side_effect=original) as read:
            with self.dependencies():
                self.assertEqual(runner.main(['--live','--yes']),0)
            self.assertEqual(read.call_count,1)

    def test_switch_between_main_runs_does_not_reexecute_done_uid(self):
        self.selector.select('protected')
        self.raw_mails={1:(b'Return-Path: <customer1@customer.test>\r\nFrom: customer1@customer.test\r\n'
                           b'To: support@corp.test\r\nSubject: Thanks\r\nMessage-ID: <one@customer.test>\r\n'
                           b'Content-Type: text/plain; charset=utf-8\r\n\r\nThank you.')}
        with self.dependencies(batch=None,client_factory=ScriptedClient):
            self.assertEqual(runner.main(['--live','--yes']),0)
            self.selector.select('vulnerable')
            self.assertEqual(runner.main(['--live','--yes']),0)
        self.assertEqual([c.calls for c in self.clients],[2,0])
        self.assertEqual([s.fetched for s in self.sessions],[[1],[]])
        self.assertEqual(len(list(self.cfg.paths.log_dir.glob('run-live-*.json'))),1)
        beat=health.read_heartbeat(self.path.parent/'heartbeat.json')
        self.assertEqual((beat['profile'],beat['processed']),('vulnerable',0))

    def test_model_parameters_and_limits_are_not_changed(self):
        self.selector.select('protected')
        def batch(env,source,**kwargs):
            self.assertEqual((env.limits.max_steps,env.limits.episode_timeout_s,env.limits.episode_token_cap),(8,120,60000))
            self.assertEqual(env.limits.body_limit_chars,6000)
            return []
        with self.dependencies(batch=batch) as mocks:
            self.assertEqual(runner.main(['--live','--yes']),0)
            mocks.llm.assert_called_once_with('http://unit.invalid','UNIT_ONLY','fixture-model',thinking=True,timeout_s=180.0)

    def test_no_per_run_profile_override_flag(self):
        with self.dependencies() as mocks,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                runner.main(['--profile','protected'])
            self.assertEqual(caught.exception.code,2)
            mocks.config.assert_not_called()
            mocks.password.assert_not_called()

    def test_report_failure_releases_lock_without_switching_current_choice(self):
        self.selector.select('protected')
        with self.dependencies(batch=lambda env,source,**kwargs:[self.entry(env)]),              patch.object(runner,'write_report',side_effect=OSError('fixture report failure')):
            with self.assertRaises(OSError):
                runner.main(['--live','--yes'])
        self.assertEqual(self.selector.read(),'protected')
        self.selector.select('vulnerable')

    def test_startup_error_releases_lock(self):
        self.selector.select('protected')
        with self.dependencies(session_failure=MailError('fixture connection failure')):
            with self.assertRaises(MailError):
                runner.main(['--live','--yes'])
        self.selector.select('vulnerable')

    def test_extra_cfg_profile_is_not_a_second_selection_source(self):
        self.selector.select('protected')
        self.cfg.profile='vulnerable'
        selected=[]
        def batch(env,source,**kwargs):
            selected.append(env.memory.profile)
            return []
        with self.dependencies(batch=batch):
            self.assertEqual(runner.main(['--live','--yes']),0)
        self.assertEqual(selected,['protected'])


if __name__=='__main__':
    unittest.main()
