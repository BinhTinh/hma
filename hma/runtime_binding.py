"""4B: chốt một context từ selector sau khi lấy khóa; không phân nhánh policy."""
from __future__ import annotations
import contextlib
from pathlib import Path
import uuid
from .memory_context import MemoryExecutionContext
from .profile_policy import ProfilePolicy
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


def context_metadata(memory: MemoryExecutionContext | None, *,
                     profile_policy: ProfilePolicy | None = None) -> dict:
    if profile_policy is not None and not isinstance(profile_policy, ProfilePolicy):
        raise ValueError('Profile policy phải do code cấp')
    if memory is None:
        if profile_policy is not None and profile_policy.name is not None:
            raise ValueError('Profile policy thực thi cần memory context cùng profile')
        return {}
    if not isinstance(memory, MemoryExecutionContext):
        raise ValueError('Context phải là MemoryExecutionContext do code cấp')
    if profile_policy is not None and profile_policy.name not in (None, memory.profile):
        raise ValueError('Profile policy không khớp context đã chốt')
    stage = profile_policy.stage if profile_policy is not None else 'binding_only'
    return {'run_id': memory.run_id, 'profile': memory.profile, 'profile_policy_stage': stage}


def validate_entry_context(entries: list[dict], memory: MemoryExecutionContext | None, *,
                           profile_policy: ProfilePolicy | None = None) -> None:
    expected = context_metadata(memory, profile_policy=profile_policy)
    if not expected:
        return
    keys = ('run_id', 'profile')
    if profile_policy is not None and profile_policy.name is not None:
        keys += ('profile_policy_stage',)
    for entry in entries:
        if any(entry.get(key) != expected[key] for key in keys):
            raise RunnerError('Không ghi report trộn hoặc thiếu context/chính sách của lượt đã chốt')
