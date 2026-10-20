"""Metadata thực thi do code cấp; không phải profile selector hoặc policy."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryExecutionContext:
    run_id: str
    profile: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip() or "\x00" in self.run_id:
            raise ValueError("run_id phải là chuỗi không rỗng, không chứa NUL")
        if not isinstance(self.profile, str) or self.profile not in ("vulnerable", "protected"):
            raise ValueError("profile phải là vulnerable hoặc protected; không có mặc định")
