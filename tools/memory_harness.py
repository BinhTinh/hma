"""3D1: harness model thật có xác nhận; database/mail giả, không sửa Agent.

Mặc định chỉ kiểm plan, không tạo client hoặc mở database.
Execute cần đồng thời --execute và --confirm-api-usage.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

from hma import db, kb, prompts, runner, store
from hma.config import load_config
from hma.llm import LLMClient, LLMError, Usage
from hma.memory_context import MemoryExecutionContext
from hma.port import CapturingPort
from hma.tools import ToolSettings
from tools.memory_preflight import build_plan


def scrub(value, secret):
    if isinstance(value, str):
        return value.replace(secret, '***') if secret else value
    if isinstance(value, list):
        return [scrub(v, secret) for v in value]
    if isinstance(value, tuple):
        return [scrub(v, secret) for v in value]
    if isinstance(value, dict):
        return {scrub(k, secret): scrub(v, secret) for k, v in value.items()}
    return value


class SecretFormatter(logging.Formatter):
    def __init__(self, secret):
        super().__init__('%(levelname)s %(name)s: %(message)s')
        self.secret = secret

    def format(self, record):
        return scrub(super().format(record), self.secret)


def write_json(path, value, secret):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(scrub(value, secret), handle, ensure_ascii=False, indent=2)
        handle.write('\n')


class EventSink:
    def __init__(self, path, secret):
        self.path, self.secret = Path(path), secret

    def __call__(self, event):
        item = dict(event, timestamp_utc=datetime.now(timezone.utc).isoformat())
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(scrub(item, self.secret), ensure_ascii=False) + '\n')
            handle.flush()


class ObservedClient(LLMClient):
    """Giữ hành vi client gốc; thêm đếm/giới hạn và giữ reference history chỉ để xuất."""
    def __init__(self, *args, chat_limit, attempt_limit, token_threshold, on_event=None, **kwargs):
        for name, value in (('chat_limit', chat_limit), ('attempt_limit', attempt_limit), ('token_threshold', token_threshold)):
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} phải là số nguyên dương')
        super().__init__(*args, **kwargs)
        self.chat_limit, self.attempt_limit, self.token_threshold = chat_limit, attempt_limit, token_threshold
        self.on_event = on_event or (lambda event: None)
        self.chat_calls = 0
        self.attempts = []
        self.returned_usage = Usage()
        self.histories = {}
        self.episode = None

    def begin_episode(self, label):
        if not isinstance(label, str) or not label.strip() or label in self.histories:
            raise ValueError('episode label không hợp lệ hoặc trùng')
        if len(self.histories) >= 2:
            raise LLMError('HARNESS_LIMIT: tối đa hai episode', retryable=False)
        self.episode = label
        self.histories[label] = None

    def chat(self, messages, tools=None):
        if self.episode is None:
            raise LLMError('HARNESS_CONTEXT: chưa bắt đầu episode', retryable=False)
        if self.chat_calls >= self.chat_limit:
            raise LLMError('HARNESS_LIMIT: hết số chat call đã chốt', retryable=False)
        if self.returned_usage.total_tokens >= self.token_threshold:
            raise LLMError('HARNESS_LIMIT: đã chạm ngưỡng token quan sát', retryable=False)
        if self.histories[self.episode] is None:
            self.histories[self.episode] = messages
        elif self.histories[self.episode] is not messages:
            raise LLMError('HARNESS_CONTEXT: history bị thay ngoài vòng đời episode', retryable=False)
        self.chat_calls += 1
        turn = super().chat(messages, tools)
        self.returned_usage = self.returned_usage + turn.usage
        return turn

    def _once(self, payload):
        if len(self.attempts) >= self.attempt_limit:
            raise LLMError('HARNESS_LIMIT: hết số adapter attempt đã chốt', retryable=False)
        record = {'attempt': len(self.attempts) + 1, 'episode': self.episode, 'outcome': 'started'}
        self.attempts.append(record)
        self.on_event(dict(record, event='attempt_started'))
        started = time.monotonic()
        try:
            turn = super()._once(payload)
        except BaseException as exc:
            record.update(outcome='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error',
                          error_type=type(exc).__name__, status=getattr(exc, 'status', None),
                          retryable=getattr(exc, 'retryable', None),
                          elapsed_ms=int((time.monotonic() - started) * 1000))
            self.on_event(dict(record, event='attempt_finished'))
            raise
        record.update(outcome='ok', finish_reason=turn.finish_reason,
                      returned_usage=asdict(turn.usage), elapsed_ms=int((time.monotonic() - started) * 1000))
        self.on_event(dict(record, event='attempt_finished'))
        return turn

    def export_traces(self):
        result = {}
        for episode, messages in self.histories.items():
            public = []
            for message in messages or []:
                item = copy.deepcopy(message)
                item.pop('reasoning_content', None)
                public.append(item)
            result[episode] = {'messages': public, 'reasoning_content_exported': False}
        return result


def validate_plan(current, approved):
    if not current.get('ready_for_harness_draft') or current.get('issues'):
        raise ValueError('preflight hiện tại chưa sẵn sàng')
    if json.dumps(current, sort_keys=True, ensure_ascii=False) != json.dumps(approved, sort_keys=True, ensure_ascii=False):
        raise ValueError('config/KB/default client khác plan đã lưu; dừng trước API')


def prepare(config_path, plan_path):
    cfg = load_config(config_path)
    docs = kb.load_kb_dir(cfg.paths.kb_dir)
    current = build_plan(cfg, docs)
    approved = json.loads(Path(plan_path).read_text(encoding='utf-8'))
    validate_plan(current, approved)
    return cfg, docs, current


def raw_email(uid, subject, body, support, run_id):
    message = EmailMessage()
    message['From'] = 'customer1@customer.test'
    message['To'] = support
    message['Return-Path'] = '<customer1@customer.test>'
    message['Subject'] = subject
    message['Message-ID'] = f'<{run_id}-{uid}@customer.test>'
    message.set_content(body)
    return message.as_bytes()


def structural_checks(entries, ticket, sender, handoff):
    first = entries[0] if entries else {}
    second = entries[1] if len(entries) > 1 else {}
    old_ids = {n['id'] for n in first.get('ticket_notes_written', [])}
    read_ids = {note_id for event in second.get('ticket_notes_read', []) for note_id in event.get('note_ids', [])}
    return {
        'first_done': first.get('status') == 'DONE',
        'first_created_one_ticket': len(first.get('tickets', [])) == 1,
        'first_wrote_one_note': len(old_ids) == 1,
        'first_handoff_to_expected': [h.get('to') for h in first.get('handoffs', [])] == [handoff],
        'first_reply_to_sender': [r.get('to') for r in first.get('replies', [])] == [sender],
        'second_ran': bool(second),
        'second_done': second.get('status') == 'DONE',
        'second_read_first_note': bool(old_ids & read_ids),
        'ticket_is_pending': bool(ticket) and ticket.get('status') == 'pending',
        'second_reply_to_sender': [r.get('to') for r in second.get('replies', [])] == [sender],
    }


def checks_status(checks):
    return 'completed_needs_review' if checks and all(checks.values()) else 'scenario_incomplete'


def execute(cfg, docs, plan, output_parent):
    run_id = 'memory-3d-' + uuid.uuid4().hex
    output = Path(output_parent).expanduser().resolve() / run_id
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    sink = EventSink(output / 'attempts.jsonl', cfg.llm.api_key)
    manifest = {'approved_preflight':plan, 'execution_run_id':run_id, 'active_live_profile':None,
                'fixture_metadata_profile_label':'protected', 'effective_policy_mode':'current_baseline_code',
                'database_scope':'new_temporary_database', 'mail_scope':'CapturingPort(None)',
                'reasoning_content_exported':False}
    write_json(output / 'plan.json', manifest, cfg.llm.api_key)
    entries, inputs = [], []
    client, conn, temporary, ticket = None, None, None, None
    status, stop_reason, code = 'execution_error', '', 2
    extra_error = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix='hma-memory-3d-')
        path = Path(temporary.name) / 'scenario.db'
        db.init_db(path)
        conn = db.connect(path)
        db.check_schema(conn)
        kb.replace_kb(conn, docs, cfg.kb_always_load)
        budget = plan['derived_plan_not_executed']
        client = ObservedClient(
            cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model,
            thinking=cfg.llm.thinking, timeout_s=cfg.llm.request_timeout_s,
            chat_limit=budget['chat_calls_max'], attempt_limit=budget['adapter_completion_attempts_max'],
            token_threshold=budget['reported_token_threshold_two_episodes'], on_event=sink,
        )
        memory = MemoryExecutionContext(run_id, 'protected')
        env = runner.Env(conn, CapturingPort(None), client, ToolSettings.from_config(cfg),
                         prompts.build_system_prompt(kb.always_load_docs(conn)), runner.Limits.from_config(cfg),
                         uidvalidity=31337, memory=memory)
        first_subject = 'Không đăng nhập được từ sáng nay'
        first_body = ('Tôi không đăng nhập được cổng khách hàng, báo lỗi ERR-AUTH-401. '
                      'Tôi đã thử xóa cache trình duyệt nhưng lỗi vẫn còn. '
                      'Nhờ hỗ trợ xử lý và ghi lại thao tác đã thử để lần sau tiếp tục theo dõi.')
        inputs.append({'episode':'email_1', 'uid':1, 'subject':first_subject, 'body':first_body})
        client.begin_episode('email_1')
        entries.append(runner.process_uid(env, 1, raw_email(1, first_subject, first_body, cfg.mail.user, run_id), None))
        first = entries[0]
        tickets = first.get('tickets', [])
        notes = first.get('ticket_notes_written', [])
        if first.get('status') != 'DONE' or len(tickets) != 1 or not notes:
            status, stop_reason = 'scenario_incomplete', 'email_1_chua_du_dieu_kien_tiep_tuc_memory'
        else:
            tid = tickets[0]['id']
            second_subject = f'Về {tid}: bổ sung thông tin'
            second_body = (f'{tid}: Tôi đang chờ xử lý và sẽ gửi thêm log trong email sau. '
                           'Bạn nhắc lại giúp tôi thao tác tôi đã thử trước đó để không làm lặp lại nhé.')
            inputs.append({'episode':'email_2', 'uid':2, 'subject':second_subject, 'body':second_body})
            client.begin_episode('email_2')
            entries.append(runner.process_uid(env, 2, raw_email(2, second_subject, second_body, cfg.mail.user, run_id), None))
            ticket = store.fetch_ticket(conn, tid)
            status = checks_status(structural_checks(entries, ticket, plan['scenario']['sender_fixture'],
                                                     plan['scenario']['handoff_fixture']))
            stop_reason = 'can_doc_noi_dung_ghi_chu_va_reply' if status == 'completed_needs_review' else 'muc_tieu_cau_truc_chua_du'
            code = 0 if status == 'completed_needs_review' else 2
    except KeyboardInterrupt:
        status, stop_reason, code = 'interrupted', 'nguoi_van_hanh_ngat; khong_tu_chay_lai', 130
    except Exception as exc:
        status, stop_reason, code = 'execution_error', 'loi_harness; khong_tu_chay_lai', 2
        extra_error = {'type':type(exc).__name__, 'message':str(exc)}
    finally:
        snapshot = {}
        if conn is not None:
            try:
                snapshot = {table:[dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                            for table in ('ticket_notes', 'tickets', 'actions', 'llm_calls')}
            except Exception as exc:
                snapshot = {'snapshot_error_type':type(exc).__name__}
            finally:
                conn.close()
        if temporary is not None:
            temporary.cleanup()
        checks = structural_checks(entries, ticket, plan['scenario']['sender_fixture'], plan['scenario']['handoff_fixture'])
        usage = asdict(client.returned_usage) if client is not None else asdict(Usage())
        attempts = client.attempts if client is not None else []
        summary = {
            'run_id':run_id, 'status':status, 'stop_reason':stop_reason,
            'episode_statuses':[{'uid':e['uid'],'status':e.get('status'),'reason':e.get('reason','')} for e in entries],
            'structural_checks':checks, 'manual_semantic_review_required':True,
            'manual_review':[
                'Ghi chú email 1 có phản ánh đúng thao tác đã thử, không bịa dữ kiện không?',
                'Reply email 2 có nhắc đúng thao tác từ ghi chú, không chỉ đoán từ KB không?',
                'Model có giữ đúng thông tin KB và thực hiện đúng yêu cầu đang chờ/bổ sung log không?',
            ],
            'chat_calls':client.chat_calls if client is not None else 0,
            'adapter_attempts_started':len(attempts),
            'usage_reported_by_successful_turns':usage,
            'usage_completeness':'not_independently_verified; failed attempts may lack usage',
            'hard_currency_cost_cap':False, 'automatic_scenario_reruns':0,
            'active_live_profile':None, 'fixture_metadata_profile_label':'protected',
            'model':cfg.llm.model, 'thinking':cfg.llm.thinking,
            'kb_source':plan['kb']['source'], 'kb_content_sha256':plan['kb']['content_sha256'],
            'kb_matches_live_database':plan['kb']['matches_live_database_kb'],
            'error':extra_error,
        }
        evidence = {'inputs':inputs, 'entries':entries, 'state_snapshot':snapshot,
                    'traces':client.export_traces() if client is not None else {}, 'attempts':attempts,
                    'reasoning_content_exported':False}
        write_json(output / 'summary.json', summary, cfg.llm.api_key)
        write_json(output / 'evidence.json', evidence, cfg.llm.api_key)
        print(json.dumps(scrub({'status':status, 'episode_statuses':summary['episode_statuses'],
                               'structural_checks':checks, 'chat_calls':summary['chat_calls'],
                               'adapter_attempts_started':len(attempts),
                               'usage_reported_by_successful_turns':usage,
                               'manual_semantic_review_required':True,
                               'report_directory':str(output)}, cfg.llm.api_key), ensure_ascii=False, indent=2))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/agent.yaml')
    parser.add_argument('--plan', required=True, help='JSON lưu từ preflight 3D0')
    parser.add_argument('--output-dir', default='.hma-runs/memory-3d')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-api-usage', action='store_true')
    args = parser.parse_args(argv)
    if args.execute and not args.confirm_api_usage:
        parser.error('--execute cần --confirm-api-usage; có thể phát sinh phí API')
    try:
        cfg, docs, plan = prepare(args.config, args.plan)
    except Exception as exc:
        print(f'DỪNG TRƯỚC API ({type(exc).__name__}): không đọc được hoặc không khớp plan. '
              'Chạy lại preflight, xem/chốt kế hoạch trước khi execute.', file=sys.stderr)
        return 2
    if not args.execute:
        print(json.dumps(dict(plan, mode='plan_only', next_action='explicit_execute_with_confirmation'),
                         ensure_ascii=False, indent=2))
        return 0
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter(cfg.llm.api_key))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    return execute(cfg, docs, plan, args.output_dir)


if __name__ == '__main__':
    sys.exit(main())
