"""3A1: adapter memory và policy nền; SQLite tạm, không gọi mail/model."""
from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hma import db, policy, store
from hma.memory_context import MemoryExecutionContext
from hma.tools import ARG_SPECS, TOOL_SCHEMAS, MessageContext, ToolSettings, Toolbox
from hma.triage import ParsedMail

SETTINGS=ToolSettings({"customer.test":"client","partner.test":"partner"},
                      {"client":"alice@corp.test","partner":"bob@corp.test"},3,20)


class FakePort:
    def __init__(self):
        self.replies,self.handoffs,self.archived=[],[],[]
    def send_reply(self, **kw):
        self.replies.append(kw)
    def send_handoff(self, **kw):
        self.handoffs.append(kw)
    def archive(self, uid):
        self.archived.append(uid)


def make_mail(**changes):
    values=dict(sender="customer1@customer.test",return_path="customer1@customer.test",return_path_present=True,
                subject="Về TKT-0001",message_id="<m1@customer.test>",references=None,
                auto_submitted=None,precedence=None,has_list_header=False,
                body_text="TKT-0001: khách bổ sung đã thử đăng nhập lại.",truncated=False)
    values.update(changes)
    return ParsedMail(**values)


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path=Path(self.tmp.name)/"tools.db"
        db.init_db(path)
        self.conn=db.connect(path)
        self.addCleanup(self.conn.close)
        self.port=FakePort()
        self.memory=MemoryExecutionContext("unit-run-01","protected")

    def toolbox(self, uid=7, mail=None, memory=True, register=True):
        mail=mail or make_mail()
        key=(store.register_message(self.conn,mailbox="INBOX",uidvalidity=111,uid=uid,
              sender=mail.sender,sender_class="client" if mail.sender.endswith("customer.test") else "partner",
              subject=mail.subject,truncated=mail.truncated) if register else "INBOX:111:999")
        context=self.memory if memory is True else (None if memory is False else memory)
        return Toolbox(MessageContext(self.conn,key,uid,mail,SETTINGS,self.port,memory=context))

    def write(self,tb,ticket="TKT-0001",body="Đã thử đăng nhập lại."):
        return tb.call("append_ticket_note",{"ticket_id":ticket,"body":body})

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM ticket_notes").fetchone()[0]

    def seed_note(self,tb,**changes):
        values=dict(ticket_id="TKT-0001",body="Thông tin đã lưu",source_message_key=tb.ctx.message_key,
                    run_id=self.memory.run_id,profile=self.memory.profile)
        values.update(changes)
        with db.transaction(self.conn):
            return store.append_ticket_note(self.conn,**values)

    def test_schema_exposes_only_ticket_and_body(self):
        schema=next(s["function"] for s in TOOL_SCHEMAS if s["function"]["name"]=="append_ticket_note")
        self.assertEqual(set(schema["parameters"]["properties"]),{"ticket_id","body"})
        self.assertEqual(set(schema["parameters"]["required"]),{"ticket_id","body"})
        self.assertFalse(schema["parameters"]["additionalProperties"])
        self.assertEqual(set(ARG_SPECS["append_ticket_note"][0]),{"ticket_id","body"})
        self.assertEqual(len(TOOL_SCHEMAS),9)

    def test_missing_context_keeps_eight_advertised_tools_and_denies_write(self):
        tb=self.toolbox(memory=False)
        self.assertEqual(len(tb.schemas_for_episode()),8)
        self.assertNotIn("append_ticket_note",{s["function"]["name"] for s in tb.schemas_for_episode()})
        self.assertNotIn("append_ticket_note",tb.call("set_task_type",{"task_type":4})["allowed_actions"])
        self.assertEqual(self.write(tb)["error"],"memory_context_required")
        self.assertEqual(self.count(),0)

    def test_explicit_context_advertises_nine_tools(self):
        tb=self.toolbox()
        self.assertEqual(len(tb.schemas_for_episode()),9)
        self.assertIn("append_ticket_note",tb.call("set_task_type",{"task_type":4})["allowed_actions"])

    def test_context_has_no_fake_default_and_rejects_invalid_values(self):
        for run,profile in (("","protected"),(None,"protected"),("x\x00","protected"),("r","intermediate"),("r",True)):
            with self.subTest(run=run,profile=profile):
                with self.assertRaises(ValueError):
                    MemoryExecutionContext(run,profile)

    def test_model_cannot_supply_provenance_fields(self):
        tb=self.toolbox()
        for field in ("requester","source_message_key","run_id","profile"):
            args={"ticket_id":"TKT-0001","body":"Thông tin",field:"forged"}
            self.assertEqual(tb.call("append_ticket_note",args)["error"],"invalid_arguments")
        self.assertEqual(self.count(),0)

    def test_missing_and_wrong_argument_types(self):
        tb=self.toolbox()
        for args in ({},{"ticket_id":"TKT-0001"},{"ticket_id":1,"body":"x"},{"ticket_id":"TKT-0001","body":True}):
            self.assertEqual(tb.call("append_ticket_note",args)["error"],"invalid_arguments")

    def test_body_validation_does_not_write(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        for body in (""," ","\n\t","x\x00y","x"*(store.NOTE_BODY_MAX_CHARS+1)):
            self.assertEqual(self.write(tb,body=body)["error"],"invalid_arguments")
        self.assertEqual(self.count(),0)

    def test_task_type_is_required(self):
        self.assertEqual(self.write(self.toolbox())["error"],"task_type_required")

    def test_truncated_mail_escalates_without_note(self):
        tb=self.toolbox(mail=make_mail(truncated=True))
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb)["error"],"needs_human_truncated")
        self.assertEqual(tb.escalation,"needs_human_truncated")
        self.assertEqual(self.count(),0)

    def test_empty_mail_escalates_without_note(self):
        tb=self.toolbox(mail=make_mail(body_text=""))
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb)["error"],"needs_human_empty_body")
        self.assertEqual(self.count(),0)

    def test_memory_permission_matrix_for_all_task_types(self):
        for number in policy.TASK_TYPES:
            with self.subTest(task_type=number):
                tb=self.toolbox(uid=100+number)
                tb.call("set_task_type",{"task_type":number})
                result=self.write(tb)
                self.assertEqual(result["ok"],number in (3,4,8))
                if number not in (3,4,8):
                    self.assertEqual(result["error"],"action_not_allowed")

    def test_required_actions_remain_original(self):
        self.assertEqual(policy.REQUIRED,{
            1:frozenset(),2:frozenset({"reply"}),3:frozenset({"create_ticket","handoff","reply"}),
            4:frozenset({"update_ticket"}),5:frozenset({"handoff"}),6:frozenset({"archive"}),
            8:frozenset({"create_ticket","handoff","reply"})})

    def test_write_uses_runtime_provenance_and_hides_it_in_response(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        result=self.write(tb)
        self.assertTrue(result["ok"])
        self.assertEqual(set(result),{"ok","ticket_id","note_id"})
        note=store.list_ticket_notes(self.conn,"TKT-0001")[0]
        self.assertEqual((note["requester"],note["source_message_key"],note["run_id"],note["profile"]),
                         (tb.mail.sender,tb.ctx.message_key,self.memory.run_id,self.memory.profile))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0],0)

    def test_other_owners_ticket_is_denied(self):
        tb=self.toolbox(mail=make_mail(sender="partner1@partner.test",return_path="partner1@partner.test"))
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb)["error"],"ticket_not_found")
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0001"})["error"],"ticket_not_found")
        self.assertEqual(self.count(),0)

    def test_ticket_not_mentioned_is_denied(self):
        tb=self.toolbox(mail=make_mail(subject="Hỏi chung",body_text="Xin hỗ trợ đăng nhập"))
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb)["error"],"ticket_not_found")

    def test_missing_ticket_is_denied(self):
        tb=self.toolbox(mail=make_mail(subject="Về TKT-9999",body_text="TKT-9999"))
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb,"TKT-9999")["error"],"ticket_not_found")

    def test_new_ticket_can_be_noted_and_read_without_id_in_original_mail(self):
        tb=self.toolbox(mail=make_mail(subject="Sự cố mới",body_text="Không đăng nhập được"))
        tb.call("set_task_type",{"task_type":3})
        ticket=tb.call("create_ticket",{"priority":"normal"})["ticket_id"]
        self.assertNotIn(ticket,tb.ids)
        self.assertTrue(self.write(tb,ticket)["ok"])
        result=tb.call("get_ticket",{"ticket_id":ticket})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["notes"]),1)

    def test_setting_self_ticket_id_is_not_authority(self):
        tb=self.toolbox(mail=make_mail(subject="Hỏi chung",body_text="Xin hỗ trợ"))
        tb.ticket_id="TKT-0001"
        tb.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(tb)["error"],"ticket_not_found")
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0001"})["error"],"ticket_not_found")

    def test_source_link_without_completed_create_action_is_not_authority(self):
        tb=self.toolbox(mail=make_mail(subject="Hỏi chung",body_text="Xin hỗ trợ"))
        self.conn.execute("UPDATE tickets SET source_message_key=? WHERE id='TKT-0001'",(tb.ctx.message_key,))
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0001"})["error"],"ticket_not_found")

    def test_create_action_without_source_link_is_not_authority(self):
        tb=self.toolbox(mail=make_mail(subject="Hỏi chung",body_text="Xin hỗ trợ"))
        store.record_action(self.conn,tb.ctx.message_key,"create_ticket","TKT-0001","DONE")
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0001"})["error"],"ticket_not_found")

    def test_created_provenance_does_not_bypass_ownership(self):
        tb=self.toolbox(mail=make_mail(subject="Hỏi chung",body_text="Xin hỗ trợ"))
        self.conn.execute("UPDATE tickets SET source_message_key=? WHERE id='TKT-0002'",(tb.ctx.message_key,))
        store.record_action(self.conn,tb.ctx.message_key,"create_ticket","TKT-0002","DONE")
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0002"})["error"],"ticket_not_found")

    def test_get_ticket_returns_only_public_note_fields_and_internal_read_trace(self):
        tb=self.toolbox()
        note=self.seed_note(tb)
        result=tb.call("get_ticket",{"ticket_id":"TKT-0001"})
        self.assertNotIn("requester",result["ticket"])
        self.assertEqual(set(result["notes"][0]),{"id","body","created_at"})
        self.assertEqual(tb.memory_reads,[{"ticket_id":"TKT-0001","note_ids":[note["id"]],"source_message_keys":[tb.ctx.message_key]}])

    def test_denied_get_does_not_read_notes(self):
        tb=self.toolbox()
        self.seed_note(tb,ticket_id="TKT-0002")
        with patch("hma.tools.store.list_ticket_notes",side_effect=AssertionError("must not read")):
            result=tb.call("get_ticket",{"ticket_id":"TKT-0002"})
        self.assertFalse(result["ok"])
        self.assertEqual(tb.memory_reads,[])

    def test_note_locks_task_type(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        self.assertTrue(self.write(tb)["ok"])
        self.assertEqual(tb.call("set_task_type",{"task_type":2})["error"],"task_type_locked")

    def test_note_does_not_complete_required_business_action(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        self.write(tb)
        self.assertEqual(tb.obligations(),["update_ticket"])

    def test_second_write_in_same_toolbox_is_denied(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        self.assertTrue(self.write(tb)["ok"])
        self.assertEqual(self.write(tb)["error"],"already_done")
        self.assertEqual(self.count(),1)

    def test_new_toolbox_same_email_cannot_duplicate_note(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        self.write(tb)
        again=self.toolbox()
        again.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(again)["error"],"already_done")
        self.assertEqual(self.count(),1)

    def test_tool_transaction_rolls_back_note_when_store_raises(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        original=store.append_ticket_note
        def failing(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError("failure after insert")
        with patch("hma.tools.store.append_ticket_note",side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.write(tb)
        self.assertEqual(self.count(),0)
        self.assertNotIn("append_ticket_note",tb.state.done)

    def test_missing_registered_email_does_not_mask_foreign_key_error(self):
        tb=self.toolbox(register=False)
        tb.call("set_task_type",{"task_type":4})
        with self.assertRaises(sqlite3.IntegrityError):
            self.write(tb)
        self.assertEqual(self.count(),0)

    def test_one_note_per_email_even_if_retry_selects_another_owned_ticket(self):
        mail=make_mail(subject="Về TKT-0001 và TKT-0003",body_text="TKT-0001 và TKT-0003")
        tb=self.toolbox(mail=mail)
        tb.call("set_task_type",{"task_type":4})
        self.write(tb)
        again=self.toolbox(mail=mail)
        again.call("set_task_type",{"task_type":4})
        self.assertEqual(self.write(again,"TKT-0003")["error"],"already_done")
        self.assertEqual(self.count(),1)

    def test_get_uses_common_note_limit(self):
        tb=self.toolbox()
        notes=[self.seed_note(tb,body=f"Ghi chú {i}") for i in range(store.NOTE_READ_DEFAULT_LIMIT+2)]
        result=tb.call("get_ticket",{"ticket_id":"TKT-0001"})
        self.assertEqual([r["id"] for r in result["notes"]],[r["id"] for r in reversed(notes)][:store.NOTE_READ_DEFAULT_LIMIT])
        self.assertEqual(tb.call("get_ticket",{"ticket_id":"TKT-0001","limit":50})["error"],"invalid_arguments")

    def test_retry_can_read_ticket_created_for_same_email(self):
        mail=make_mail(subject="Sự cố mới",body_text="Không đăng nhập được")
        tb=self.toolbox(mail=mail)
        tb.call("set_task_type",{"task_type":3})
        ticket=tb.call("create_ticket",{"priority":"normal"})["ticket_id"]
        again=self.toolbox(mail=mail)
        self.assertTrue(again.call("get_ticket",{"ticket_id":ticket})["ok"])

    def test_read_does_not_lock_task_type(self):
        tb=self.toolbox()
        tb.call("set_task_type",{"task_type":4})
        tb.call("get_ticket",{"ticket_id":"TKT-0001"})
        self.assertTrue(tb.call("set_task_type",{"task_type":2})["ok"])

    def test_both_metadata_labels_keep_same_baseline_ownership_check(self):
        for index,profile in enumerate(("vulnerable","protected")):
            tb=self.toolbox(uid=30+index,memory=MemoryExecutionContext("fixture-only",profile),
                            mail=make_mail(sender="partner1@partner.test",return_path="partner1@partner.test"))
            tb.call("set_task_type",{"task_type":4})
            self.assertEqual(self.write(tb)["error"],"ticket_not_found")


if __name__=='__main__':
    unittest.main()
