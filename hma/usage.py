"""Ghi usage từng lần gọi LLM vào bảng llm_calls (giai đoạn 3)."""
from __future__ import annotations

import sqlite3

from .db import now_iso
from .llm import AssistantTurn


def record_llm_call(conn: sqlite3.Connection, message_key: str, step: int, turn: AssistantTurn) -> None:
    u = turn.usage
    conn.execute(
        "INSERT INTO llm_calls(message_key, step, prompt_tokens, completion_tokens, total_tokens, reasoning_tokens,"
        " cache_hit_tokens, cache_miss_tokens, latency_ms, finish_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (message_key, step, u.prompt_tokens, u.completion_tokens, u.total_tokens, u.reasoning_tokens,
         u.cache_hit_tokens, u.cache_miss_tokens, turn.latency_ms, turn.finish_reason, now_iso()))
