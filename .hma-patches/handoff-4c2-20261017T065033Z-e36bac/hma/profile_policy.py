"""Chính sách thực thi lấy từ tên profile đã chốt; 4C1 chỉ ownership.

name=None chỉ dành cho fixture/utility baseline cũ, không phải lựa chọn vận hành.
main luôn cấp tên từ context đọc dưới khóa; không có cờ ownership từ YAML/model.
"""
from __future__ import annotations
from dataclasses import dataclass
from .profiles import PROFILE_NAMES


@dataclass(frozen=True)
class ProfilePolicy:
    name: str | None = None

    def __post_init__(self):
        if self.name is not None and (not isinstance(self.name, str) or self.name not in PROFILE_NAMES):
            raise ValueError('Profile policy không hợp lệ')

    @property
    def enforce_ticket_ownership(self) -> bool:
        return self.name != 'vulnerable'

    @property
    def stage(self) -> str:
        return 'binding_only' if self.name is None else 'ownership_only'


BASELINE_POLICY = ProfilePolicy()


def policy_for_profile(name: str | None = None) -> ProfilePolicy:
    return BASELINE_POLICY if name is None else ProfilePolicy(name)
