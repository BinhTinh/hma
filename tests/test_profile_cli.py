"""Profile CLI trên cfg fixture; không load YAML thật, không mở database/mail/model."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from hma import cli
from hma.profiles import ProfileSelector
from hma.runtime_lock import RunLock


class ProfileCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=Path(self.tmp.name)/'data'/'agent.db'
        self.cfg=SimpleNamespace(paths=SimpleNamespace(db=self.db))
        self.selector=ProfileSelector(self.db)

    def call(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with patch.object(cli,'load_config',return_value=self.cfg),              contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=cli.main(list(args))
        return code,out.getvalue(),err.getvalue()

    def test_show_missing_does_not_choose_or_create_files(self):
        code,out,err=self.call('profile','show')
        self.assertEqual(code,0,err)
        result=json.loads(out)
        self.assertIsNone(result['selected_profile'])
        self.assertEqual(result['selection_state'],'not_selected')
        self.assertFalse(result['runtime_profile_reported'])
        self.assertFalse(self.db.parent.exists())

    def test_select_and_show_use_same_file(self):
        code,out,err=self.call('profile','select','vulnerable')
        self.assertEqual(code,0,err)
        result=json.loads(out)
        self.assertTrue(result['changed'])
        self.assertEqual(result['selected_profile'],'vulnerable')
        code,out,err=self.call('profile','show')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['state_file'],str(self.selector.state_path))
        self.assertEqual(json.loads(out)['selected_profile'],'vulnerable')
        self.assertFalse(self.db.exists())

    def test_cli_can_store_other_valid_name(self):
        code,out,err=self.call('profile','select','protected')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['selected_profile'],'protected')
        self.assertEqual(json.loads(out)['scope'],'4A_selector_storage_only')

    def test_repeat_selection_reports_no_change(self):
        self.call('profile','select','protected')
        code,out,err=self.call('profile','select','protected')
        self.assertEqual(code,0,err)
        self.assertFalse(json.loads(out)['changed'])

    def test_busy_selector_returns_error_and_preserves_state(self):
        self.selector.select('vulnerable')
        with RunLock(self.selector.lock_path):
            code,out,err=self.call('profile','select','protected')
        self.assertEqual(code,2)
        self.assertEqual(json.loads(err)['error'],'run_lock_busy')
        self.assertEqual(self.selector.read(),'vulnerable')

    def test_corrupt_state_is_not_reported_as_no_selection(self):
        self.selector.directory.mkdir()
        self.selector.state_path.write_text('{broken')
        code,out,err=self.call('profile','show')
        self.assertEqual(code,2)
        self.assertEqual(json.loads(err)['error'],'profile_state_error')

    def test_invalid_cli_name_is_rejected_by_parser(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.call('profile','select','auto')
        self.assertEqual(caught.exception.code,2)
        self.assertFalse(self.db.parent.exists())

    def test_missing_subcommand_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.call('profile')
        self.assertEqual(caught.exception.code,2)

    def test_cfg_profile_attribute_is_not_an_override(self):
        self.selector.select('vulnerable')
        self.cfg.profile='protected'
        code,out,err=self.call('profile','show')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['selected_profile'],'vulnerable')

    def test_profile_commands_never_open_or_initialize_database(self):
        error=AssertionError('profile CLI must not touch SQLite')
        with patch.object(cli.db,'connect',side_effect=error),              patch.object(cli.db,'connect_existing',side_effect=error),              patch.object(cli.db,'init_db',side_effect=error):
            self.assertEqual(self.call('profile','show')[0],0)
            self.assertEqual(self.call('profile','select','protected')[0],0)

    def test_show_can_observe_choice_while_runtime_lock_is_held(self):
        self.selector.select('protected')
        with RunLock(self.selector.lock_path):
            code,out,err=self.call('profile','show')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['selected_profile'],'protected')


if __name__=='__main__':
    unittest.main()
