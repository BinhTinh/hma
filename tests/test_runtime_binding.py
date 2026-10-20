"""4B runtime binding: selector/khóa thật trên thư mục tạm, không API/SQLite."""
from __future__ import annotations
import contextlib
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from hma import runtime_binding as B
from hma.memory_context import MemoryExecutionContext
from hma.profiles import ProfileSelector
from hma.runtime_lock import RunBusy, RunLock, RunnerError


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'data'/'agent.db'
        self.selector=ProfileSelector(self.path)

    def test_explicit_selection_is_bound_without_creating_database(self):
        self.selector.select('protected')
        with contextlib.ExitStack() as stack:
            memory=B.acquire_runtime_context(stack,self.path)
            self.assertEqual(memory.profile,'protected')
            self.assertTrue(memory.run_id.startswith('hma-run-'))
        self.assertFalse(self.path.exists())

    def test_missing_selection_is_error_not_fallback_or_uuid(self):
        with patch.object(B.uuid,'uuid4') as uuid_factory:
            with self.assertRaises(RunnerError):
                with contextlib.ExitStack() as stack:
                    B.acquire_runtime_context(stack,self.path)
            uuid_factory.assert_not_called()
        self.assertFalse(self.selector.state_path.exists())
        with RunLock(self.selector.lock_path):
            pass

    def test_bad_selection_is_error_before_context_creation(self):
        self.selector.directory.mkdir(parents=True)
        self.selector.state_path.write_text('{broken')
        with patch.object(B.uuid,'uuid4') as factory:
            with self.assertRaises(RunnerError):
                with contextlib.ExitStack() as stack:
                    B.acquire_runtime_context(stack,self.path)
            factory.assert_not_called()

    def test_if_idle_busy_returns_none_without_reading_selection(self):
        with RunLock(self.selector.lock_path):
            with patch.object(B.ProfileSelector,'read_locked') as read, patch.object(B.uuid,'uuid4') as factory:
                with contextlib.ExitStack() as stack:
                    self.assertIsNone(B.acquire_runtime_context(stack,self.path,if_idle=True))
                read.assert_not_called()
                factory.assert_not_called()

    def test_busy_without_if_idle_is_error(self):
        with RunLock(self.selector.lock_path):
            with self.assertRaises(RunBusy):
                with contextlib.ExitStack() as stack:
                    B.acquire_runtime_context(stack,self.path)

    def test_lock_stays_held_until_owner_stack_exits(self):
        self.selector.select('protected')
        with contextlib.ExitStack() as stack:
            memory=B.acquire_runtime_context(stack,self.path)
            with self.assertRaises(RunBusy):
                self.selector.select('vulnerable')
            self.assertEqual(memory.profile,'protected')
        self.selector.select('vulnerable')

    def test_uuid_is_created_while_the_shared_lock_is_held(self):
        self.selector.select('protected')
        def make_uuid():
            with self.assertRaises(RunBusy):
                self.selector.select('vulnerable')
            return SimpleNamespace(hex='fixed-fixture-id')
        with patch.object(B.uuid,'uuid4',side_effect=make_uuid) as factory:
            with contextlib.ExitStack() as stack:
                memory=B.acquire_runtime_context(stack,self.path)
                self.assertEqual(memory.run_id,'hma-run-fixed-fixture-id')
            self.assertEqual(factory.call_count,1)

    def test_context_is_immutable(self):
        self.selector.select('protected')
        with contextlib.ExitStack() as stack:
            memory=B.acquire_runtime_context(stack,self.path)
            with self.assertRaises(FrozenInstanceError):
                memory.profile='vulnerable'

    def test_new_run_can_use_new_choice_without_mutating_old_context(self):
        self.selector.select('vulnerable')
        with contextlib.ExitStack() as stack:
            first=B.acquire_runtime_context(stack,self.path)
        self.selector.select('protected')
        with contextlib.ExitStack() as stack:
            second=B.acquire_runtime_context(stack,self.path)
        self.assertNotEqual(first.run_id,second.run_id)
        self.assertEqual((first.profile,second.profile),('vulnerable','protected'))

    def test_selection_is_read_once_not_again_for_metadata(self):
        self.selector.select('protected')
        original=B.ProfileSelector.read
        with patch.object(B.ProfileSelector,'read',autospec=True,side_effect=original) as read:
            with contextlib.ExitStack() as stack:
                memory=B.acquire_runtime_context(stack,self.path)
                for _ in range(3):
                    B.context_metadata(memory)
            self.assertEqual(read.call_count,1)

    def test_metadata_is_new_dict_and_states_policy_binding_only(self):
        memory=MemoryExecutionContext('fixture','protected')
        first=B.context_metadata(memory)
        first['profile']='changed'
        self.assertEqual(B.context_metadata(memory),{'run_id':'fixture','profile':'protected','profile_policy_stage':'binding_only'})

    def test_none_context_is_legacy_utility_not_a_selected_profile(self):
        self.assertEqual(B.context_metadata(None),{})
        B.validate_entry_context([{'uid':1}],None)

    def test_invalid_context_type_is_rejected(self):
        with self.assertRaises(ValueError):
            B.context_metadata(SimpleNamespace(run_id='x',profile='protected'))

    def test_mixed_or_missing_entry_identity_is_rejected(self):
        memory=MemoryExecutionContext('fixture','protected')
        good=B.context_metadata(memory)
        B.validate_entry_context([dict(good,uid=1)],memory)
        for wrong in ({'run_id':'other','profile':'protected'}, {'run_id':'fixture','profile':'vulnerable'}, {}):
            with self.subTest(wrong=wrong):
                with self.assertRaises(RunnerError):
                    B.validate_entry_context([good,wrong],memory)


if __name__=='__main__':
    unittest.main()
