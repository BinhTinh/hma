"""4B: chốt một context từ selector sau khi lấy khóa; không phân nhánh policy."""
from __future__ import annotations
import contextlib
from pathlib import Path
import uuid
from .memory_context import MemoryExecutionContext
from .profiles import ProfileError, ProfileSelector
from .runtime_lock import RunBusy, RunLock, RunnerError


def acquire_runtime_context(stack: contextlib.ExitStack, db_path: str | Path,
                            if_idle: bool = False) -> MemoryExecutionContext | None:
    selector = ProfileSelector(db_path)
    try:
        guard = stack.enter_context(RunLock(selector.lock_path))
    except RunBusy:
        if if_idle:
            return None
        raise
    try:
        profile = selector.read_locked(guard)
    except ProfileError as exc:
        raise RunnerError(f'Không chốt được profile: {exc}; kiểm tra bằng hma profile show') from None
    return MemoryExecutionContext('hma-run-' + uuid.uuid4().hex, profile)


def context_metadata(memory: MemoryExecutionContext | None) -> dict:
    if memory is None:
        return {}
    if not isinstance(memory, MemoryExecutionContext):
        raise ValueError('Context phải là MemoryExecutionContext do code cấp')
    return {'run_id': memory.run_id, 'profile': memory.profile, 'profile_policy_stage': 'binding_only'}


def validate_entry_context(entries: list[dict], memory: MemoryExecutionContext | None) -> None:
    expected = context_metadata(memory)
    if not expected:
        return
    for entry in entries:
        if any(entry.get(key) != expected[key] for key in ('run_id', 'profile')):
            raise RunnerError('Không ghi report trộn hoặc thiếu run_id/profile của lượt đã chốt')
