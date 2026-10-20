"""Client LLM (giai đoạn 3): POST {base_url}/chat/completions qua gateway. Chỉ dùng thư viện chuẩn."""
from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 4_000_000
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
TRANSIENT_FINISH = frozenset({"insufficient_system_resource", "aborted"})


class LLMError(RuntimeError):
    """Lỗi gọi LLM. retryable=True nghĩa là thử lại sau có thể thành công. Thông báo không chứa khóa API."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(*(a + b for a, b in zip(self.__dict__.values(), other.__dict__.values())))


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str          # chuỗi JSON thô do model sinh; có thể không hợp lệ


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None
    reasoning_content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: Usage
    latency_ms: int = 0

    def to_message(self) -> dict:
        """Thông điệp assistant để đưa lại vào lịch sử. PHẢI giữ reasoning_content khi dùng thinking + tools."""
        msg: dict = {"role": "assistant", "content": self.content if (self.content is not None or self.tool_calls) else ""}
        if self.reasoning_content is not None:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}}
                                 for t in self.tool_calls]
        return msg


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_usage(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt, completion = _int(raw.get("prompt_tokens")), _int(raw.get("completion_tokens"))
    details = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    hit = _int(raw.get("prompt_cache_hit_tokens")) or _int(prompt_details.get("cached_tokens"))
    miss = _int(raw.get("prompt_cache_miss_tokens"))
    return Usage(prompt, completion, _int(raw.get("total_tokens")) or (prompt + completion),
                 _int(details.get("reasoning_tokens")), hit, miss)


def parse_response(body: bytes, latency_ms: int) -> AssistantTurn:
    try:
        data = json.loads(body)
        choice = data["choices"][0]
        message = choice["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"phản hồi LLM sai cấu trúc: {type(exc).__name__}") from exc
    if not isinstance(message, dict):
        raise LLMError("phản hồi LLM sai cấu trúc: message không phải đối tượng")
    calls = []
    for tc in message.get("tool_calls") or []:
        try:
            fn = tc["function"]
            args = fn.get("arguments", "")
            calls.append(ToolCall(str(tc["id"]), str(fn["name"]), args if isinstance(args, str) else json.dumps(args)))
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError(f"tool_call sai cấu trúc: {type(exc).__name__}") from exc
    content, reasoning = message.get("content"), message.get("reasoning_content")
    return AssistantTurn(content if isinstance(content, str) else None, reasoning if isinstance(reasoning, str) else None,
                         tuple(calls), str(choice.get("finish_reason") or "stop"), parse_usage(data.get("usage")), latency_ms)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, *, thinking: bool = True,
                 reasoning_effort: str | None = None, timeout_s: float = 180.0, max_retries: int = 2,
                 backoff_s: float = 1.0, max_tokens: int | None = None,
                 sleep: Callable[[float], None] = time.sleep, opener: urllib.request.OpenerDirector | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"base_url phải là http(s)://host[:port], nhận {base_url!r}")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.model, self.thinking, self.reasoning_effort = model, thinking, reasoning_effort
        self.timeout_s, self.max_retries, self.backoff_s = timeout_s, max_retries, backoff_s
        self.max_tokens, self._sleep = max_tokens, sleep
        # ProxyHandler({}) để gateway nội bộ không bị biến môi trường http_proxy chen vào.
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> bytes:
        body: dict = {"model": self.model, "messages": messages, "stream": False,
                      "thinking": {"type": "enabled" if self.thinking else "disabled"}}
        if tools:
            body["tools"] = tools
        if self.thinking and self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _once(self, payload: bytes) -> AssistantTurn:
        req = urllib.request.Request(self.url, data=payload, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        t0 = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            snippet = exc.read(300).decode("utf-8", "replace").replace(self._key, "***")
            raise LLMError(f"LLM trả HTTP {exc.code}: {snippet}", retryable=exc.code in RETRYABLE_STATUS,
                           status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as exc:
            raise LLMError(f"không gọi được LLM ({type(exc).__name__})", retryable=True) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise LLMError("phản hồi LLM quá lớn")
        turn = parse_response(body, int((time.monotonic() - t0) * 1000))
        if turn.finish_reason in TRANSIENT_FINISH:
            raise LLMError(f"LLM ngắt giữa chừng ({turn.finish_reason})", retryable=True)
        if turn.finish_reason == "content_filter":
            raise LLMError("LLM từ chối vì bộ lọc nội dung")
        return turn

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> AssistantTurn:
        payload = self._payload(messages, tools)
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._once(payload)
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt == self.max_retries:
                    break
                delay = self.backoff_s * (2 ** attempt)
                log.warning("LLM lỗi tạm thời (%s); thử lại sau %.1fs", exc, delay)
                self._sleep(delay)
        assert last is not None
        raise last
