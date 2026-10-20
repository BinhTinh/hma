"""4A: một nguồn lựa chọn profile trên mỗi thư mục dữ liệu HMA.

Không mở SQLite, không đọc environment để chọn profile và không chọn thay người vận hành.
Runner sẽ tiêu thụ lựa chọn ở 4B; module này chưa phân nhánh policy.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from .runtime_lock import RunLock

PROFILE_NAMES = ('vulnerable', 'protected')
FORMAT_VERSION = 1
MAX_STATE_BYTES = 4096


class ProfileError(RuntimeError):
    """Lựa chọn thiếu/hỏng hoặc tham số không hợp lệ; không được tự fallback."""


class ProfileNotSelected(ProfileError):
    """Chưa có lựa chọn; không đồng nghĩa với protected hoặc vulnerable."""


@dataclass(frozen=True)
class SelectionChange:
    selected_profile: str
    changed: bool


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError('JSON lựa chọn có khóa lặp')
        result[key] = value
    return result


def _validate_name(name):
    if not isinstance(name, str) or name not in PROFILE_NAMES:
        raise ProfileError('profile phải là vulnerable hoặc protected')
    return name


class ProfileSelector:
    def __init__(self, db_path: str | Path):
        # Chỉ resolve thư mục cha: giống nơi runner hiện đặt run.lock.
        # Không follow symlink của chính file database để chọn một thư mục khóa khác.
        self.directory = Path(db_path).expanduser().parent.resolve()
        self.state_path = self.directory / 'selected_profile.json'
        self.lock_path = self.directory / 'run.lock'

    def read(self) -> str:
        try:
            with self.state_path.open('rb') as handle:
                raw = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            raise ProfileNotSelected('Chưa chọn profile; không tự chọn mặc định') from None
        except OSError as exc:
            raise ProfileError('Không đọc được file lựa chọn profile') from exc
        if len(raw) > MAX_STATE_BYTES:
            raise ProfileError('File lựa chọn profile quá lớn')
        try:
            obj = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileError('File lựa chọn profile không phải JSON UTF-8 hợp lệ') from exc
        if not isinstance(obj, dict) or set(obj) != {'format_version', 'profile'}:
            raise ProfileError('Schema file lựa chọn profile không hợp lệ')
        if type(obj['format_version']) is not int or obj['format_version'] != FORMAT_VERSION:
            raise ProfileError('Không hỗ trợ version của file lựa chọn profile')
        return _validate_name(obj['profile'])

    def read_locked(self, guard: RunLock) -> str:
        """Dành cho runner 4B: chỉ đọc sau khi đã lấy đúng khóa dùng chung."""
        if not isinstance(guard, RunLock) or not guard.held:
            raise ProfileError('Cần giữ RunLock trước khi chốt profile')
        if guard.path.resolve() != self.lock_path.resolve():
            raise ProfileError('RunLock không thuộc cùng thư mục dữ liệu')
        return self.read()

    def select(self, name: str) -> SelectionChange:
        name = _validate_name(name)
        with RunLock(self.lock_path):
            try:
                previous = self.read()
            except ProfileNotSelected:
                previous = None
            if previous == name:
                return SelectionChange(name, False)
            self._write(name)
            return SelectionChange(name, True)

    def _write(self, name: str) -> None:
        payload = json.dumps({'format_version': FORMAT_VERSION, 'profile': name},
                             ensure_ascii=False, indent=2) + '\n'
        fd, filename = tempfile.mkstemp(prefix='.selected-profile-', suffix='.tmp', dir=self.directory)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()
