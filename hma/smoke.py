"""Chạy thử LLM thật trên vài thư mẫu, với CSDL tạm và cổng thư GIẢ (không gửi thư, không đụng hộp thư thật).

Chạy: python -m hma.smoke [--config config/agent.yaml] [--only M07]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db, kb, prompts, store
from .loop import run_episode
from .tools import TOOL_SCHEMAS, MessageContext, ToolSettings, Toolbox
from .triage import ParsedMail
from .usage import record_llm_call


class RecordingPort:
    def __init__(self) -> None:
        self.replies: list[dict] = []
        self.handoffs: list[dict] = []
        self.archived: list[int] = []

    def send_reply(self, **kw) -> None:
        self.replies.append(kw)

    def send_handoff(self, **kw) -> None:
        self.handoffs.append(kw)

    def archive(self, uid: int) -> None:
        self.archived.append(uid)


def _mail(sender: str, subject: str, body: str) -> ParsedMail:
    return ParsedMail(sender=sender, return_path=sender, return_path_present=True, subject=subject,
                      message_id=f"<smoke-{abs(hash(subject)) % 10**6}@test>", references=None, auto_submitted=None,
                      precedence=None, has_list_header=False, body_text=body, truncated=False)


@dataclass
class Case:
    name: str
    uid: int
    mail: ParsedMail
    check: Callable[[dict], list[str]]   # nhận kết quả quan sát, trả danh sách vấn đề


def _check_m04(o: dict) -> list[str]:
    p = []
    if len(o["replies"]) != 1:
        p.append(f"cần đúng 1 reply, có {len(o['replies'])}")
    else:
        body = o["replies"][0]["body"]
        p += [f"reply thiếu {s!r}" for s in ("08:00", "17:00") if s not in body]
    if o["tickets"] or o["handoffs"] or o["archived"]:
        p.append("loại 2 không được tạo ticket/chuyển giao/lưu Archive")
    return p


def _check_m07(o: dict) -> list[str]:
    p = []
    if len(o["tickets"]) != 1:
        return [f"cần đúng 1 ticket, có {len(o['tickets'])}"]
    t = o["tickets"][0]
    if t["priority"] != "normal":
        p.append(f"priority phải là normal, nhận {t['priority']}")
    if [h["to"] for h in o["handoffs"]] != ["alice@corp.test"]:
        p.append(f"phải chuyển cho alice, nhận {[h['to'] for h in o['handoffs']]}")
    if len(o["replies"]) != 1 or t["id"] not in o["replies"][0]["body"]:
        p.append("reply phải có đúng 1 thư và chứa mã ticket")
    return p


def _check_m08(o: dict) -> list[str]:
    p = []
    if len(o["tickets"]) != 1:
        return [f"cần đúng 1 ticket, có {len(o['tickets'])}"]
    if o["tickets"][0]["priority"] != "high":
        p.append(f"priority phải là high (ảnh hưởng toàn bộ dịch vụ, 'khẩn' không phải urgent), nhận {o['tickets'][0]['priority']}")
    if [h["to"] for h in o["handoffs"]] != ["bob@corp.test"]:
        p.append(f"phải chuyển cho bob, nhận {[h['to'] for h in o['handoffs']]}")
    if len(o["replies"]) != 1:
        p.append("cần đúng 1 reply")
    return p


def _check_m16(o: dict) -> list[str]:
    p = []
    if o["archived"] != [16]:
        p.append(f"phải lưu Archive UID 16, nhận {o['archived']}")
    if o["replies"] or o["handoffs"] or o["tickets"]:
        p.append("bản tin không được reply/ticket/chuyển giao")
    return p


CASES = [
    Case("M04", 4, _mail("customer1@customer.test", "Giờ làm việc của bộ phận hỗ trợ?",
                         "Chào bạn,\n\nBộ phận hỗ trợ làm việc vào những giờ nào trong tuần ạ?\n\nCảm ơn,\nKhách hàng 1"), _check_m04),
    Case("M07", 7, _mail("customer1@customer.test", "Không đăng nhập được từ sáng nay",
                         "Chào bạn,\n\nTừ sáng nay tôi không đăng nhập được cổng khách hàng, hệ thống báo lỗi ERR-AUTH-401. "
                         "Chỉ có tài khoản của tôi bị ảnh hưởng.\n\nCảm ơn,\nKhách hàng 1"), _check_m07),
    Case("M08", 8, _mail("partner1@partner.test", "API trả về lỗi 500 liên tục",
                         "Xin chào,\n\nTất cả lời gọi API đặt hàng của chúng tôi đang trả về lỗi 500 từ 09:00, toàn bộ đơn hàng "
                         "bị ảnh hưởng. Cần xử lý khẩn.\n\nTrân trọng,\nĐối tác 1"), _check_m08),
    Case("M16", 16, _mail("partner1@partner.test", "Bản tin đối tác tháng 10",
                          "Kính gửi quý đối tác,\n\nĐây là bản tin định kỳ tháng 10 của chúng tôi. Không cần phản hồi.\n\nĐối tác 1"),
         _check_m16),
]


def run_smoke(cfg, llm, docs, say: Callable[[str], None] = print, only: str | None = None) -> int:
    cases = [c for c in CASES if only is None or c.name == only]
    if not cases:
        say(f"Không có ca mẫu '{only}'. Ca có sẵn: {[c.name for c in CASES]}")
        return 2
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "smoke.db"
        db.init_db(path)
        conn = db.connect(path)
        try:
            kb.replace_kb(conn, docs, cfg.kb_always_load)
            system_prompt = prompts.build_system_prompt(kb.always_load_docs(conn))
            settings = ToolSettings.from_config(cfg)
            say(f"Chạy {len(cases)} ca mẫu với LLM thật; CSDL tạm, cổng thư giả (không gửi thư).")
            for c in cases:
                port = RecordingPort()
                key = store.register_message(conn, mailbox="SMOKE", uidvalidity=1, uid=c.uid, sender=c.mail.sender,
                                             sender_class=None, subject=c.mail.subject, truncated=False)
                tb = Toolbox(MessageContext(conn, key, c.uid, c.mail, settings, port))
                res = run_episode(llm, tb, TOOL_SCHEMAS, system_prompt, prompts.build_task_message(c.uid),
                                  max_steps=cfg.agent.max_steps, timeout_s=cfg.agent.episode_timeout_s,
                                  token_cap=cfg.agent.episode_token_cap,
                                  on_call=lambda s, t, k=key: record_llm_call(conn, k, s, t))
                trace = []
                results = {m["tool_call_id"]: json.loads(m["content"]) for m in res.messages if m["role"] == "tool"}
                for m in res.messages:
                    for tc in m.get("tool_calls", []) if m["role"] == "assistant" else []:
                        r = results.get(tc["id"], {})
                        trace.append(f"{tc['function']['name']}({tc['function']['arguments'][:70]})"
                                     f"{'' if r.get('ok') else ' -> LỖI ' + str(r.get('error'))}")
                tickets = [dict(r) for r in conn.execute("SELECT id, priority, status FROM tickets WHERE source_message_key = ?", (key,))]
                obs = {"replies": port.replies, "handoffs": port.handoffs, "archived": port.archived, "tickets": tickets}
                problems = [] if res.status == "done" else [f"episode kết thúc '{res.status}': {res.reason}"]
                problems += c.check(obs)
                u = res.usage
                say(f"\n== {c.name} (UID {c.uid}) | {res.status} | {res.steps} bước | prompt {u.prompt_tokens} "
                    f"(cache {u.cache_hit_tokens}) completion {u.completion_tokens} (suy luận {u.reasoning_tokens})")
                say("   tool: " + " -> ".join(trace))
                for t in tickets:
                    say(f"   ticket: {t}")
                for h in port.handoffs:
                    say(f"   chuyển giao tới {h['to']}: {h['subject']}")
                for r in port.replies:
                    say(f"   reply tới {r['to']}: {r['body'][:260]!r}")
                if port.archived:
                    say(f"   archive UID: {port.archived}")
                say(f"   tóm tắt của model: {res.final_text[:200]!r}")
                say("   KẾT QUẢ: " + ("ĐẠT" if not problems else "CẦN XEM: " + "; ".join(problems)))
                failed += bool(problems)
        finally:
            conn.close()
    say(f"\nTổng: {len(cases) - failed}/{len(cases)} ca đạt.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    from .config import load_config
    from .llm import LLMClient

    ap = argparse.ArgumentParser(prog="python -m hma.smoke", description=__doc__)
    ap.add_argument("--config", default="config/agent.yaml")
    ap.add_argument("--only", help="chỉ chạy một ca, ví dụ M07")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    docs = kb.load_kb_dir(cfg.paths.kb_dir)
    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model, thinking=cfg.llm.thinking,
                    timeout_s=cfg.llm.request_timeout_s)
    return run_smoke(cfg, llm, docs, print, args.only)


if __name__ == "__main__":
    sys.exit(main())
