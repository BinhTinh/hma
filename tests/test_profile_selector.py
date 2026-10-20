"""Selector storage: file tạm; không mở SQLite, không chọn profile thật."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from hma import profiles as P
from hma.runtime_lock import RunBusy,RunLock

ROOT=Path(__file__).resolve().parent.parent


class ProfileSelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.db=self.base/'data'/'agent.db'
        self.selector=P.ProfileSelector(self.db)

    def write_raw(self,value):
        self.selector.directory.mkdir(parents=True,exist_ok=True)
        self.selector.state_path.write_bytes(value)

    def test_fixed_paths_share_existing_runner_lock_directory(self):
        self.assertEqual(self.selector.state_path,self.db.parent/'selected_profile.json')
        self.assertEqual(self.selector.lock_path,self.db.parent/'run.lock')

    def test_missing_selection_has_no_default(self):
        with self.assertRaises(P.ProfileNotSelected):
            self.selector.read()

    def test_missing_read_does_not_create_directory_or_lock(self):
        with self.assertRaises(P.ProfileNotSelected):
            self.selector.read()
        self.assertFalse(self.db.parent.exists())

    def test_first_selection_creates_only_state_and_lock_not_database(self):
        change=self.selector.select('vulnerable')
        self.assertTrue(change.changed)
        self.assertEqual(change.selected_profile,'vulnerable')
        self.assertEqual(self.selector.read(),'vulnerable')
        self.assertFalse(self.db.exists())
        self.assertEqual({p.name for p in self.db.parent.iterdir()},{'selected_profile.json','run.lock'})

    def test_state_format_is_small_explicit_and_versioned(self):
        self.selector.select('protected')
        state=json.loads(self.selector.state_path.read_text())
        self.assertEqual(state,{'format_version':1,'profile':'protected'})

    def test_switch_replaces_same_authority_file(self):
        self.selector.select('vulnerable')
        change=self.selector.select('protected')
        self.assertTrue(change.changed)
        self.assertEqual(self.selector.read(),'protected')
        self.assertEqual({p.name for p in self.db.parent.iterdir()},{'selected_profile.json','run.lock'})

    def test_same_selection_is_noop_under_lock(self):
        self.selector.select('protected')
        before=self.selector.state_path.read_bytes()
        stamp=self.selector.state_path.stat().st_mtime_ns
        change=self.selector.select('protected')
        self.assertFalse(change.changed)
        self.assertEqual(self.selector.state_path.read_bytes(),before)
        self.assertEqual(self.selector.state_path.stat().st_mtime_ns,stamp)

    def test_invalid_names_do_not_create_state_or_directory(self):
        for value in ('','Protected','baseline','auto',True,1,None,[]):
            with self.subTest(value=value):
                with self.assertRaises(P.ProfileError):
                    self.selector.select(value)
        self.assertFalse(self.db.parent.exists())

    def test_invalid_json_or_schema_is_not_fallback(self):
        bad=[b'',b'{',b'[]',b'null',b'{"profile":"protected"}',
             b'{"format_version":1,"profile":"auto"}',
             b'{"format_version":true,"profile":"protected"}',
             b'{"format_version":1.0,"profile":"protected"}',
             b'{"format_version":2,"profile":"protected"}',
             b'{"format_version":1,"profile":"protected","extra":1}',
             b'\xff']
        for raw in bad:
            with self.subTest(raw=raw):
                self.write_raw(raw)
                with self.assertRaises(P.ProfileError):
                    self.selector.read()
                self.assertEqual(self.selector.state_path.read_bytes(),raw)

    def test_duplicate_json_keys_are_rejected(self):
        self.write_raw(b'{"format_version":1,"profile":"vulnerable","profile":"protected"}')
        with self.assertRaises(P.ProfileError):
            self.selector.read()

    def test_oversize_state_is_rejected(self):
        self.write_raw(b' '*(P.MAX_STATE_BYTES+1))
        with self.assertRaises(P.ProfileError):
            self.selector.read()

    def test_whitespace_does_not_create_another_authority(self):
        self.write_raw(b'  {"profile":"protected","format_version":1} \n')
        self.assertEqual(self.selector.read(),'protected')

    def test_corrupt_existing_state_is_not_silently_repaired_by_select(self):
        self.write_raw(b'{"format_version":999,"profile":"protected"}')
        before=self.selector.state_path.read_bytes()
        with self.assertRaises(P.ProfileError):
            self.selector.select('vulnerable')
        self.assertEqual(self.selector.state_path.read_bytes(),before)

    def test_live_lock_blocks_switch_and_preserves_previous_selection(self):
        self.selector.select('vulnerable')
        before=self.selector.state_path.read_bytes()
        with RunLock(self.selector.lock_path):
            with self.assertRaises(RunBusy):
                self.selector.select('protected')
        self.assertEqual(self.selector.state_path.read_bytes(),before)

    def test_live_lock_blocks_even_same_selection_request(self):
        self.selector.select('protected')
        with RunLock(self.selector.lock_path):
            with self.assertRaises(RunBusy):
                self.selector.select('protected')

    def test_read_can_inspect_committed_state_while_lock_is_held(self):
        self.selector.select('protected')
        with RunLock(self.selector.lock_path):
            self.assertEqual(self.selector.read(),'protected')

    def test_replace_failure_keeps_old_state_and_removes_temporary_file(self):
        self.selector.select('vulnerable')
        before=self.selector.state_path.read_bytes()
        with patch.object(P.os,'replace',side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                self.selector.select('protected')
        self.assertEqual(self.selector.state_path.read_bytes(),before)
        self.assertEqual(list(self.selector.directory.glob('.selected-profile-*.tmp')),[])

    def test_fsync_failure_keeps_old_state_and_removes_temporary_file(self):
        self.selector.select('vulnerable')
        before=self.selector.state_path.read_bytes()
        with patch.object(P.os,'fsync',side_effect=OSError('fixture sync failure')):
            with self.assertRaises(OSError):
                self.selector.select('protected')
        self.assertEqual(self.selector.state_path.read_bytes(),before)
        self.assertEqual(list(self.selector.directory.glob('.selected-profile-*.tmp')),[])

    def test_first_write_failure_has_no_implicit_selected_profile(self):
        with patch.object(P.os,'replace',side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                self.selector.select('protected')
        with self.assertRaises(P.ProfileNotSelected):
            self.selector.read()

    def test_read_locked_requires_current_matching_guard(self):
        self.selector.select('protected')
        guard=RunLock(self.selector.lock_path)
        with self.assertRaises(P.ProfileError):
            self.selector.read_locked(guard)
        with guard:
            self.assertEqual(self.selector.read_locked(guard),'protected')
        with self.assertRaises(P.ProfileError):
            self.selector.read_locked(guard)

    def test_read_locked_refuses_different_lock_path(self):
        self.selector.select('protected')
        with RunLock(self.base/'other'/'run.lock') as guard:
            with self.assertRaises(P.ProfileError):
                self.selector.read_locked(guard)

    def test_read_locked_missing_selection_does_not_default(self):
        with RunLock(self.selector.lock_path) as guard:
            with self.assertRaises(P.ProfileNotSelected):
                self.selector.read_locked(guard)

    def test_database_contents_are_not_read_or_changed(self):
        self.db.parent.mkdir()
        self.db.write_bytes(b'not a sqlite file; must remain unchanged')
        before=self.db.read_bytes()
        self.selector.select('protected')
        self.assertEqual(self.db.read_bytes(),before)

    def test_database_file_symlink_does_not_move_the_lock_directory(self):
        self.db.parent.mkdir()
        other=self.base/'other'
        other.mkdir()
        destination=other/'external.db'
        destination.write_bytes(b'do not open')
        self.db.symlink_to(destination)
        selector=P.ProfileSelector(self.db)
        self.assertEqual(selector.lock_path,self.db.parent/'run.lock')
        selector.select('protected')
        self.assertEqual(destination.read_bytes(),b'do not open')
        self.assertFalse((other/'selected_profile.json').exists())

    def test_environment_profile_is_not_an_override(self):
        self.selector.select('vulnerable')
        with patch.dict(os.environ,{'HMA_PROFILE':'protected','ACTIVE_PROFILE':'protected'}):
            self.assertEqual(self.selector.read(),'vulnerable')

    def test_independent_selector_process_cannot_switch_during_run_lock(self):
        self.selector.select('vulnerable')
        script="""import sys
from hma.profiles import ProfileSelector
from hma.runtime_lock import RunBusy
try:
    ProfileSelector(sys.argv[1]).select('protected')
except RunBusy:
    print('busy')
    sys.exit(7)
"""
        with RunLock(self.selector.lock_path):
            result=subprocess.run([sys.executable,'-B','-c',script,str(self.db)],cwd=ROOT,
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual((result.returncode,result.stdout.strip()),(7,'busy'),result.stderr)
        self.assertEqual(self.selector.read(),'vulnerable')


if __name__=='__main__':
    unittest.main()
