"""3D0: đọc config/KB và in kế hoạch; không gọi model hoặc mở database.

Chạy: python3 -B preflight_memory_3d.py --config config/agent.yaml
Không có cờ execute trong file này.
"""
from __future__ import annotations
import argparse
import hashlib
import inspect
import json
import sys
from urllib.parse import urlparse

from hma.config import load_config
from hma.kb import load_kb_dir
from hma.llm import LLMClient


def client_defaults():
    parameters = inspect.signature(LLMClient.__init__).parameters
    return {name: parameters[name].default for name in
            ('max_retries', 'backoff_s', 'reasoning_effort', 'max_tokens')}


def kb_manifest(docs, always_load):
    records = sorted(
        ({'doc_id': d.doc_id, 'title': d.title, 'body': d.body} for d in docs),
        key=lambda d: d['doc_id'],
    )
    ids = [d['doc_id'] for d in records]
    data = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {
        'source': 'configured_markdown_files',
        'matches_live_database_kb': 'not_verified',
        'document_count': len(records),
        'document_ids': ids,
        'always_load': list(always_load),
        'missing_always_load': sorted(set(always_load) - set(ids)),
        'content_sha256': hashlib.sha256(data).hexdigest(),
        'body_included_in_output': False,
    }


def build_plan(cfg, docs):
    defaults = client_defaults()
    retries = defaults['max_retries']
    if type(retries) is not int or retries < 0:
        raise ValueError('client default max_retries không hợp lệ')
    parsed = urlparse(cfg.llm.base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('LLM endpoint không hợp lệ')
    kb = kb_manifest(docs, cfg.kb_always_load)
    issues = []
    if not kb['document_count']:
        issues.append('KB file không có tài liệu')
    if kb['missing_always_load']:
        issues.append('Thiếu tài liệu KB luôn nạp')
    if cfg.sender_classes.get('customer.test') != 'client':
        issues.append('Fixture customer1@customer.test không thuộc class client trong config hiện tại')
    if not cfg.handoff.get('client'):
        issues.append('Không có mapping handoff cho client')
    if cfg.agent.max_run_messages < 2:
        issues.append('max_run_messages nhỏ hơn 2; cần chốt lại phạm vi trước khi chạy chuỗi hai email')
    episodes = 2
    chat_limit = episodes * cfg.agent.max_steps
    return {
        'stage': '3D0_preflight_only',
        'ready_for_harness_draft': not issues,
        'issues': issues,
        'api_called': False,
        'database_opened': False,
        'mail_transport_opened': False,
        'agent_source_changed': False,
        'scenario': {
            'chains': 1,
            'episodes_max': episodes,
            'sender_fixture': 'customer1@customer.test',
            'handoff_fixture': cfg.handoff.get('client'),
            'state_for_future_harness': 'temporary_SQLite_and_CapturingPort_None',
            'memory_context': 'explicit_test_fixture_not_live_profile_selection',
            'automatic_scenario_reruns': 0,
        },
        'llm': {
            'endpoint_scheme': parsed.scheme,
            'endpoint_host': parsed.hostname,
            'endpoint_port': parsed.port,
            'model': cfg.llm.model,
            'thinking': cfg.llm.thinking,
            'request_timeout_s': cfg.llm.request_timeout_s,
            'max_retries_default': retries,
            'backoff_s_default': defaults['backoff_s'],
            'reasoning_effort_default': defaults['reasoning_effort'],
            'max_tokens_default': defaults['max_tokens'],
            'api_key_included': False,
        },
        'agent_limits': {
            'max_steps_per_episode': cfg.agent.max_steps,
            'episode_timeout_s': cfg.agent.episode_timeout_s,
            'episode_token_cap': cfg.agent.episode_token_cap,
            'body_limit_chars': cfg.agent.body_limit_chars,
            'kb_top_k': cfg.agent.kb_top_k,
            'max_run_messages': cfg.agent.max_run_messages,
            'reply_daily_cap': cfg.agent.max_replies_per_recipient_per_day,
        },
        'derived_plan_not_executed': {
            'chat_calls_max': chat_limit,
            'adapter_completion_attempts_max': chat_limit * (retries + 1),
            'reported_token_threshold_two_episodes': episodes * cfg.agent.episode_token_cap,
            'episode_timeout_sum_s': episodes * cfg.agent.episode_timeout_s,
            'hard_currency_cost_cap': False,
            'hard_inflight_token_cap': defaults['max_tokens'] is not None,
            'hard_wall_clock_deadline': False,
        },
        'kb': kb,
        'limitations': [
            'Giới hạn token của loop được kiểm trước lượt gọi kế tiếp, không hủy request đang chạy.',
            'Timeout episode không tự cắt một request blocking; request giữ timeout trong config.',
            'Attempt lỗi/timeout có thể không có usage trong response; không suy ra tổng chi phí chính xác.',
            'Số adapter attempts không phải bằng chứng gateway/provider đã xử lý hoặc tính phí bao nhiêu request.',
            'max_tokens khác None chỉ là giới hạn output request, không phải trần tổng token hoặc tiền.',
            'KB đọc từ file cấu hình như dry-run hiện tại; chưa đối chiếu KB trong database live.',
            'Chưa có API call, kết quả model, profile live hoặc đánh giá bảo mật trong preflight.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/agent.yaml')
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        docs = load_kb_dir(cfg.paths.kb_dir)
        plan = build_plan(cfg, docs)
    except Exception as exc:
        print(f'LỖI preflight ({type(exc).__name__}); chưa gọi API hoặc mở database. '
              'Kiểm tra config, đường dẫn KB và phiên bản source.', file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan['ready_for_harness_draft'] else 2


if __name__ == '__main__':
    sys.exit(main())
