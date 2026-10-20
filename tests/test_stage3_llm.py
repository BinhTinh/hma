"""Kiểm thử client LLM với máy chủ HTTP giả chạy cục bộ (giai đoạn 3)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hma.llm import AssistantTurn, LLMClient, LLMError, ToolCall, Usage, parse_response, parse_usage

OK_BODY = {
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "đang suy nghĩ",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_message", "arguments": "{}"}}]}}],
    "usage": {"prompt_tokens": 1637, "completion_tokens": 50, "total_tokens": 1687,
              "prompt_cache_hit_tokens": 1600, "prompt_cache_miss_tokens": 37,
              "completion_tokens_details": {"reasoning_tokens": 12}},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        srv = self.server
        srv.requests.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(body)})
        status, payload, delay = srv.script.pop(0) if srv.script else (200, OK_BODY, 0)
        if delay:
            time.sleep(delay)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.requests, self.srv.script = [], []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.sleeps = []

    def client(self, **kw):
        kw.setdefault("sleep", self.sleeps.append)
        return LLMClient(self.base, "FAKE-KEY", "deepseek-flash", **kw)


class RequestShapeTests(ServerCase):
    def test_payload_and_headers(self):
        tools = [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}]
        self.client(reasoning_effort="high").chat([{"role": "user", "content": "xin chào"}], tools)
        r = self.srv.requests[0]
        self.assertEqual(r["path"], "/chat/completions")
        self.assertEqual(r["headers"]["Authorization"], "Bearer FAKE-KEY")
        j = r["json"]
        self.assertEqual((j["model"], j["stream"], j["thinking"], j["reasoning_effort"]),
                         ("deepseek-flash", False, {"type": "enabled"}, "high"))
        self.assertEqual(j["tools"], tools)
        self.assertEqual(j["messages"][0]["content"], "xin chào")
        for banned in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens"):
            self.assertNotIn(banned, j)

    def test_thinking_disabled_drops_effort(self):
        self.client(thinking=False, reasoning_effort="high", max_tokens=500).chat([{"role": "user", "content": "x"}])
        j = self.srv.requests[0]["json"]
        self.assertEqual(j["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", j)
        self.assertNotIn("tools", j)
        self.assertEqual(j["max_tokens"], 500)

    def test_reasoning_content_is_sent_back(self):
        turn = self.client().chat([{"role": "user", "content": "x"}])
        msgs = [{"role": "user", "content": "x"}, turn.to_message(),
                {"role": "tool", "tool_call_id": "call_1", "content": "{}"}]
        self.client().chat(msgs)
        sent = self.srv.requests[1]["json"]["messages"][1]
        self.assertEqual(sent["reasoning_content"], "đang suy nghĩ")
        self.assertEqual(sent["tool_calls"][0]["function"]["name"], "read_message")

    def test_bad_base_url(self):
        for bad in ("ftp://x", "llm.corp.test:8080", ""):
            with self.assertRaises(ValueError):
                LLMClient(bad, "k", "m")


class ParseTests(ServerCase):
    def test_parse_turn_and_usage(self):
        t = self.client().chat([{"role": "user", "content": "x"}])
        self.assertEqual(t.finish_reason, "tool_calls")
        self.assertEqual(t.tool_calls, (ToolCall("call_1", "read_message", "{}"),))
        self.assertEqual(t.usage, Usage(1637, 50, 1687, 12, 1600, 37))
        self.assertEqual(t.reasoning_content, "đang suy nghĩ")
        m = t.to_message()
        self.assertIsNone(m["content"])
        self.assertEqual(m["tool_calls"][0]["type"], "function")

    def test_to_message_plain_answer(self):
        t = AssistantTurn(None, None, (), "stop", Usage())
        self.assertEqual(t.to_message(), {"role": "assistant", "content": ""})

    def test_usage_variants(self):
        self.assertEqual(parse_usage(None), Usage())
        self.assertEqual(parse_usage({"prompt_tokens": 5, "completion_tokens": 3}).total_tokens, 8)
        self.assertEqual(parse_usage({"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}).cache_hit_tokens, 4)
        self.assertEqual(parse_usage({"prompt_tokens": True}).prompt_tokens, 0)
        self.assertEqual(Usage(1, 2, 3, 4, 5, 6) + Usage(1, 1, 1, 1, 1, 1), Usage(2, 3, 4, 5, 6, 7))

    def test_arguments_given_as_object_are_serialised(self):
        body = {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "n", "arguments": {"k": 1}}}]}}]}
        t = parse_response(json.dumps(body).encode(), 0)
        self.assertEqual(json.loads(t.tool_calls[0].arguments), {"k": 1})

    def test_malformed_bodies(self):
        for body in (b"not json", b"{}", b'{"choices": []}', b'{"choices": [{"message": 5}]}',
                     b'{"choices": [{"message": {"tool_calls": [{"id": "a"}]}}]}'):
            with self.assertRaises(LLMError, msg=body):
                parse_response(body, 0)


class RetryTests(ServerCase):
    def test_retries_then_succeeds(self):
        self.srv.script = [(503, {"error": "busy"}, 0), (429, {"error": "slow"}, 0)]
        t = self.client(backoff_s=0.5).chat([{"role": "user", "content": "x"}])
        self.assertEqual(t.finish_reason, "tool_calls")
        self.assertEqual(len(self.srv.requests), 3)
        self.assertEqual(self.sleeps, [0.5, 1.0])

    def test_exhausted_retries_is_retryable_error(self):
        self.srv.script = [(500, {}, 0)] * 3
        with self.assertRaises(LLMError) as cm:
            self.client(max_retries=2).chat([{"role": "user", "content": "x"}])
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(self.srv.requests), 3)

    def test_client_errors_are_not_retried_and_do_not_leak_key(self):
        for status in (400, 401, 422):
            self.srv.requests.clear()
            self.srv.script = [(status, {"error": "Authorization Bearer FAKE-KEY rejected"}, 0)]
            with self.assertRaises(LLMError) as cm:
                self.client().chat([{"role": "user", "content": "x"}])
            self.assertFalse(cm.exception.retryable)
            self.assertEqual(len(self.srv.requests), 1)
            self.assertNotIn("FAKE-KEY", str(cm.exception))

    def test_transient_finish_reason_is_retried(self):
        busy = {"choices": [{"finish_reason": "insufficient_system_resource", "message": {"content": ""}}]}
        self.srv.script = [(200, busy, 0)]
        t = self.client().chat([{"role": "user", "content": "x"}])
        self.assertEqual(t.finish_reason, "tool_calls")
        self.assertEqual(len(self.srv.requests), 2)

    def test_content_filter_is_final(self):
        self.srv.script = [(200, {"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]}, 0)]
        with self.assertRaises(LLMError) as cm:
            self.client().chat([{"role": "user", "content": "x"}])
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(len(self.srv.requests), 1)

    def test_timeout_is_retryable(self):
        self.srv.script = [(200, OK_BODY, 1.0)]
        with self.assertRaises(LLMError) as cm:
            self.client(timeout_s=0.2, max_retries=0).chat([{"role": "user", "content": "x"}])
        self.assertTrue(cm.exception.retryable)

    def test_connection_refused_is_retryable(self):
        c = LLMClient("http://127.0.0.1:1", "k", "m", max_retries=1, sleep=self.sleeps.append)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "x"}])
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.sleeps), 1)


if __name__ == "__main__":
    unittest.main()
