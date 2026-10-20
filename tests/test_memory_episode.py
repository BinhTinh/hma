"""3C: hai episode qua runner/loop/tool/store thật, client có kịch bản.

Không gọi API model, IMAP/SMTP, không đọc config hoặc database live.
Kịch bản kiểm đường dữ liệu và nghĩa vụ, không đo khả năng hiểu của LLM.
"""
from __future__ import annotations
import copy
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from hma import db, kb, prompts, runner, store
from hma.kb import KBDoc
from hma.llm import Usage
from hma.memory_context import MemoryExecutionContext
from hma.port import CapturingPort
from hma.tools import ToolSettings
from hma.triage import parse_mail

MARKER = "Đã thử xóa cache trình duyệt; lỗi ERR-AUTH-401 vẫn còn."
SETTINGS = ToolSettings(
    {"customer.test": "client", "partner.test": "partner"},
    {"client": "alice@corp.test", "partner": "bob@corp.test"}, 3, 20,
)


@dataclass
class ScriptedCall:
    id: str
    name: str
    arguments: str


@dataclass
class ScriptedTurn:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    latency_ms: float = 0.0

    def to_message(self):
        message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return message


class ScriptedClient:
    """Chỉ thực thi kế hoạch của fixture; không tự hiểu hoặc sinh suy luận."""
    def __init__(self, name, plan):
        self.name, self.plan, self.index = name, list(plan), 0
        self.histories, self.schemas_seen = [], []

    def chat(self, messages, tools=None):
        self.histories.append(copy.deepcopy(messages))
        self.schemas_seen.append(copy.deepcopy(tools or []))
        if self.index >= len(self.plan):
            raise AssertionError("Kịch bản hết bước nhưng loop vẫn yêu cầu phản hồi")
        item = self.plan[self.index]
        self.index += 1
        if callable(item):
            item = item(messages)
        if isinstance(item, str):
            return ScriptedTurn(content=item)
        name, arguments = item
        available = {s["function"]["name"] for s in (tools or [])}
        if name not in available:
            raise AssertionError(f"Kịch bản gọi tool không được cấp: {name}")
        call = ScriptedCall(f"{self.name}-{self.index}", name, json.dumps(arguments, ensure_ascii=False))
        return ScriptedTurn(tool_calls=[call], finish_reason="tool_calls")


def tool_result(messages, name):
    names = {}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                names[call["id"]] = call["function"]["name"]
    matches = [json.loads(m["content"]) for m in messages
               if m.get("role") == "tool" and names.get(m.get("tool_call_id")) == name]
    if not matches:
        raise AssertionError(f"Chưa có kết quả tool {name} trong history của episode này")
    result = matches[-1]
    if not result.get("ok"):
        raise AssertionError(f"Tool {name} không thành công: {result}")
    return result


def new_ticket_note(messages):
    ticket = tool_result(messages, "create_ticket")["ticket_id"]
    return "append_ticket_note", {"ticket_id": ticket, "body": MARKER}


def reply_using_previous_note(messages):
    notes = tool_result(messages, "get_ticket")["notes"]
    if not notes:
        raise AssertionError("Episode 2 không nhận được ghi chú từ get_ticket")
    return "reply", {"body": "Đã ghi nhận bạn sẽ bổ sung log. Bối cảnh đã lưu: " + notes[0]["body"]}


def reply_using_kb(messages):
    hits = tool_result(messages, "search_kb")["results"]
    doc = next((h for h in hits if h["doc_id"] == "KB-001"), None)
    if doc is None:
        raise AssertionError("Không nhận được KB giờ làm việc")
    return "reply", {"body": doc["body"]}


def raw_email(uid, subject, body):
    message = EmailMessage()
    message["From"] = "customer1@customer.test"
    message["To"] = "support@corp.test"
    message["Return-Path"] = "<customer1@customer.test>"
    message["Subject"] = subject
    message["Message-ID"] = f"<memory-episode-{uid}@customer.test>"
    message.set_content(body)
    return message.as_bytes()


def first_plan(include_note=True):
    plan = [
        ("read_message", {}),
        ("search_kb", {"query": "lỗi đăng nhập"}),
        ("set_task_type", {"task_type": 3}),
        ("create_ticket", {"priority": "normal"}),
    ]
    if include_note:
        plan.append(new_ticket_note)
    plan += [
        ("route", {"target": "handoff"}),
        ("reply", {"body": "Chúng tôi đã ghi nhận sự cố và chuyển bộ phận phụ trách."}),
        "Đã xử lý email sự cố.",
    ]
    return plan


def second_plan(ticket):
    return [
        ("read_message", {}),
        ("get_ticket", {"ticket_id": ticket}),
        ("set_task_type", {"task_type": 4}),
        ("update_ticket", {"ticket_id": ticket, "status": "pending"}),
        ("append_ticket_note", {"ticket_id": ticket, "body": "Khách đang chờ xử lý và sẽ bổ sung log."}),
        reply_using_previous_note,
        "Đã cập nhật ticket và trả lời email bổ sung.",
    ]


class MemoryEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "episode-test.db"
        db.init_db(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        kb.replace_kb(self.conn, [
            KBDoc("KB-001", "Giờ làm việc của bộ phận hỗ trợ", "Làm việc từ thứ Hai đến thứ Sáu, 08:00 đến 17:00."),
            KBDoc("KB-018", "Mức ưu tiên", "Ảnh hưởng một người dùng: normal; ảnh hưởng nhiều người dùng: high."),
        ], ("KB-018",))

    def env(self, client, memory):
        limits = runner.Limits(8, 30, 1000, 10000, 10)
        system = prompts.build_system_prompt(kb.always_load_docs(self.conn))
        return runner.Env(self.conn, CapturingPort(None), client, SETTINGS, system, limits,
                          uidvalidity=111, memory=memory)

    def pair(self, reconnect=False):
        first = ScriptedClient("email1", first_plan())
        env1 = self.env(first, MemoryExecutionContext("scenario-run-01", "protected"))
        raw1 = raw_email(7, "Không đăng nhập được", "Tôi không đăng nhập được. " + MARKER)
        entry1 = runner.process_uid(env1, 7, raw1, None)
        self.assertEqual(entry1["status"], "DONE", entry1)
        self.assertEqual(len(entry1["tickets"]), 1)
        ticket = entry1["tickets"][0]["id"]
        if reconnect:
            self.conn.close()
            self.conn = db.connect(self.path)
            self.addCleanup(self.conn.close)
        second = ScriptedClient("email2", second_plan(ticket))
        env2 = self.env(second, MemoryExecutionContext("scenario-run-02", "protected"))
        raw2 = raw_email(8, f"Về {ticket}", f"{ticket}: Tôi sẽ gửi thêm log; hiện đang chờ xử lý.")
        entry2 = runner.process_uid(env2, 8, raw2, None)
        self.assertEqual(entry2["status"], "DONE", entry2)
        return dict(first=first, second=second, env1=env1, env2=env2,
                    entry1=entry1, entry2=entry2, ticket=ticket, raw1=raw1, raw2=raw2)

    def test_two_emails_complete_through_real_runner_loop_tools_and_store(self):
        result = self.pair()
        first, second = result["entry1"], result["entry2"]
        self.assertEqual(first["handoffs"][0]["to"], "alice@corp.test")
        self.assertEqual(first["replies"][0]["to"], "customer1@customer.test")
        self.assertEqual(second["handoffs"], [])
        self.assertEqual(second["replies"][0]["to"], "customer1@customer.test")
        ticket = store.fetch_ticket(self.conn, result["ticket"])
        self.assertEqual((ticket["status"], ticket["priority"]), ("pending", "normal"))
        self.assertEqual((first["steps"], second["steps"]), (8, 7))
        self.assertEqual(len(store.list_ticket_notes(self.conn, result["ticket"])), 2)

    def test_second_email_reply_uses_note_not_repeated_in_email(self):
        result = self.pair()
        second_body = parse_mail(result["raw2"], 10000).body_text
        self.assertNotIn(MARKER, second_body)
        self.assertIn(MARKER, result["entry2"]["replies"][0]["body"])

    def test_second_episode_has_fresh_history_and_receives_note_via_tool(self):
        result = self.pair()
        histories = result["second"].histories
        self.assertEqual([m["role"] for m in histories[0]], ["system", "user"])
        self.assertNotIn(MARKER, json.dumps(histories[0], ensure_ascii=False))
        read_result = tool_result(histories[2], "get_ticket")
        self.assertEqual(read_result["notes"][0]["body"], MARKER)
        self.assertEqual(read_result["ticket"]["status"], "open")

    def test_note_survives_reconnect_and_new_run_context(self):
        result = self.pair(reconnect=True)
        notes = store.list_ticket_notes(self.conn, result["ticket"])
        self.assertEqual([n["run_id"] for n in notes], ["scenario-run-02", "scenario-run-01"])
        self.assertEqual(notes[1]["body"], MARKER)
        self.assertEqual(notes[1]["source_message_key"], result["entry1"]["key"])
        self.assertEqual(notes[0]["source_message_key"], result["entry2"]["key"])
        self.assertIn(MARKER, result["entry2"]["replies"][0]["body"])

    def test_report_links_second_read_to_first_write(self):
        result = self.pair()
        first, second = result["entry1"], result["entry2"]
        old_id = first["ticket_notes_written"][0]["id"]
        self.assertEqual(second["ticket_notes_read"], [{
            "ticket_id": result["ticket"], "note_ids": [old_id], "source_message_keys": [first["key"]],
        }])
        self.assertEqual(len(second["ticket_notes_written"]), 1)
        self.assertNotEqual(second["ticket_notes_written"][0]["id"], old_id)
        self.assertEqual(second["ticket_notes_written"][0]["source_message_key"], second["key"])

    def test_same_prompt_and_schema_are_used_across_episodes(self):
        result = self.pair()
        self.assertEqual(result["env1"].system_prompt, result["env2"].system_prompt)
        schemas = result["first"].schemas_seen + result["second"].schemas_seen
        self.assertTrue(schemas)
        self.assertTrue(all(s == schemas[0] for s in schemas))
        self.assertEqual(len(schemas[0]), 9)
        self.assertNotIn("scenario-run-01", result["env1"].system_prompt)
        self.assertNotIn("scenario-run-02", result["env2"].system_prompt)

    def test_missing_context_keeps_normal_incident_working_without_notes(self):
        client = ScriptedClient("no-context", first_plan(include_note=False))
        env = self.env(client, None)
        entry = runner.process_uid(env, 7, raw_email(7, "Không đăng nhập", "Tôi không đăng nhập được."), None)
        self.assertEqual(entry["status"], "DONE", entry)
        self.assertTrue(all(len(s) == 8 for s in client.schemas_seen))
        self.assertEqual(entry["ticket_notes_written"], [])
        self.assertEqual(len(entry["tickets"]), 1)
        self.assertEqual(len(entry["handoffs"]), 1)
        self.assertEqual(len(entry["replies"]), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0], 0)

    def test_note_cannot_replace_required_update_in_real_loop(self):
        def update_after_nudge(messages):
            self.assertEqual(messages[-1]["role"], "user")
            self.assertIn("update_ticket", messages[-1]["content"])
            return "update_ticket", {"ticket_id": "TKT-0001", "status": "pending"}
        client = ScriptedClient("obligations", [
            ("read_message", {}), ("get_ticket", {"ticket_id": "TKT-0001"}),
            ("set_task_type", {"task_type": 4}),
            ("append_ticket_note", {"ticket_id": "TKT-0001", "body": "Khách sẽ bổ sung log."}),
            "Đã lưu ghi chú.", update_after_nudge, "Đã cập nhật ticket.",
        ])
        env = self.env(client, MemoryExecutionContext("obligation-fixture", "protected"))
        entry = runner.process_uid(env, 7, raw_email(7, "Về TKT-0001", "TKT-0001: Tôi sẽ gửi thêm log."), None)
        self.assertEqual(entry["status"], "DONE", entry)
        self.assertEqual(client.index, 7)
        self.assertEqual(store.fetch_ticket(self.conn, "TKT-0001")["status"], "pending")
        self.assertEqual(len(entry["ticket_notes_written"]), 1)
        self.assertTrue(any(a["action_type"] == "update_ticket" for a in entry["actions"]))

    def test_kb_only_email_does_not_create_ticket_or_note(self):
        client = ScriptedClient("kb-only", [
            ("read_message", {}), ("search_kb", {"query": "giờ làm việc"}),
            ("set_task_type", {"task_type": 2}), reply_using_kb, "Đã trả lời câu hỏi.",
        ])
        env = self.env(client, MemoryExecutionContext("kb-fixture", "protected"))
        entry = runner.process_uid(env, 7, raw_email(7, "Giờ làm việc", "Bộ phận hỗ trợ làm việc vào giờ nào?"), None)
        self.assertEqual(entry["status"], "DONE", entry)
        self.assertEqual(entry["tickets"], [])
        self.assertEqual(entry["handoffs"], [])
        self.assertEqual(entry["ticket_notes_written"], [])
        self.assertIn("08:00 đến 17:00", entry["replies"][0]["body"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
