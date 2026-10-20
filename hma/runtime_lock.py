"""Khóa chạy dùng chung cho runner live và thao tác đổi lựa chọn profile.

Không unlink/replace run.lock; chỉ khóa/mở khóa trên cùng đường dẫn ổn định.
"""
from __future__ import annotations
import errno
import fcntl
from pathlib import Path


class RunnerError(RuntimeError):
    """Lỗi cấp lần chạy: dừng cả lần chạy, không xử lý tiếp."""


class RunBusy(RunnerError):
    """Khóa đang do một đường thực thi khác giữ."""


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path, self._fh = Path(path), None

    @property
    def held(self) -> bool:
        return self._fh is not None and not self._fh.closed

    def __enter__(self) -> 'RunLock':
        if self.held:
            raise RunnerError('Không lấy lại cùng một đối tượng RunLock đang giữ khóa')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RunBusy(f'đang có một tiến trình khác giữ khóa {self.path}') from None
            raise
        self._fh = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
