"""Khóa chung POSIX: thư mục tạm, không khởi động runner hoặc gọi model."""
from __future__ import annotations
import contextlib
import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from hma import runner
from hma import runtime_lock as R

ROOT = Path(__file__).resolve().parent.parent


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'data'/'run.lock'

    def test_runner_reexports_shared_class_and_error(self):
        self.assertIs(runner.RunLock,R.RunLock)
        self.assertIs(runner.RunnerError,R.RunnerError)

    def test_held_state_and_lock_file_persists_after_release(self):
        lock=R.RunLock(self.path)
        self.assertFalse(lock.held)
        with lock:
            self.assertTrue(lock.held)
        self.assertFalse(lock.held)
        self.assertTrue(self.path.is_file())

    def test_does_not_truncate_existing_lock_file(self):
        self.path.parent.mkdir()
        self.path.write_text('operator-marker')
        with R.RunLock(self.path):
            self.assertEqual(self.path.read_text(),'operator-marker')
        self.assertEqual(self.path.read_text(),'operator-marker')

    def test_second_handle_is_busy(self):
        with R.RunLock(self.path):
            with self.assertRaises(R.RunBusy):
                with R.RunLock(self.path):
                    pass

    def test_failed_handle_can_be_used_after_release(self):
        other=R.RunLock(self.path)
        with R.RunLock(self.path):
            with self.assertRaises(R.RunBusy):
                other.__enter__()
            self.assertFalse(other.held)
        with other:
            self.assertTrue(other.held)

    def test_same_object_cannot_reenter_or_release_outer_lock(self):
        lock=R.RunLock(self.path)
        with lock:
            with self.assertRaises(R.RunnerError):
                lock.__enter__()
            self.assertTrue(lock.held)
            with self.assertRaises(R.RunBusy):
                R.RunLock(self.path).__enter__()

    def test_non_contention_oserror_is_not_reported_as_busy(self):
        lock=R.RunLock(self.path)
        with patch.object(R.fcntl,'flock',side_effect=OSError(errno.EIO,'fixture I/O error')):
            with self.assertRaises(OSError):
                lock.__enter__()
        self.assertFalse(lock.held)

    def test_both_documented_contention_errno_values_are_busy(self):
        for code in (errno.EACCES,errno.EAGAIN):
            with self.subTest(code=code):
                with patch.object(R.fcntl,'flock',side_effect=OSError(code,'busy')):
                    with self.assertRaises(R.RunBusy):
                        R.RunLock(self.path).__enter__()

    def test_existing_acquire_lock_if_idle_contract_is_preserved(self):
        with R.RunLock(self.path):
            with contextlib.ExitStack() as stack:
                self.assertFalse(runner.acquire_lock(stack,self.path,True))
            with contextlib.ExitStack() as stack:
                with self.assertRaises(runner.RunnerError):
                    runner.acquire_lock(stack,self.path,False)
        with contextlib.ExitStack() as stack:
            self.assertTrue(runner.acquire_lock(stack,self.path,False))

    def test_independent_process_observes_same_lock(self):
        script = """import sys
from pathlib import Path
from hma.runtime_lock import RunLock, RunBusy
try:
    with RunLock(Path(sys.argv[1])):
        print('acquired')
except RunBusy:
    print('busy')
    sys.exit(7)
"""
        command=[sys.executable,'-B','-c',script,str(self.path)]
        with R.RunLock(self.path):
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual((result.returncode,result.stdout.strip()),(7,'busy'),result.stderr)
        result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual((result.returncode,result.stdout.strip()),(0,'acquired'),result.stderr)


if __name__=='__main__':
    unittest.main()
