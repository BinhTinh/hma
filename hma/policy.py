"""Chính sách hành động (giai đoạn 2a): ma trận loại tác vụ và kiểm tra từng hành động.

Mọi hàm ở đây thuần logic: nhận trạng thái + dữ kiện, trả Decision. Việc thực thi nằm ở giai đoạn 2b.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .triage import ParsedMail, reply_block

TASK_TYPES = (1, 2, 3, 4, 5, 6, 8)
PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("open", "pending", "resolved", "closed")
WRITE_ACTIONS = ("create_ticket", "update_ticket", "reply", "handoff", "archive", "append_ticket_note")

# Hành động ghi được phép theo loại tác vụ (lấy từ ground truth + CR-18/19).
ALLOWED: dict[int, frozenset[str]] = {
    1: frozenset(),
    2: frozenset({"reply"}),
    3: frozenset({"create_ticket", "handoff", "reply", "append_ticket_note"}),
    4: frozenset({"update_ticket", "handoff", "reply", "append_ticket_note"}),
    5: frozenset({"handoff", "reply"}),
    6: frozenset({"archive"}),
    8: frozenset({"create_ticket", "handoff", "reply", "append_ticket_note"}),
}
# Hành động bắt buộc phải xong thì thư mới coi là hoàn tất (handoff của loại 4 là động, xem needs_handoff).
REQUIRED: dict[int, frozenset[str]] = {
    1: frozenset(),
    2: frozenset({"reply"}),
    3: frozenset({"create_ticket", "handoff", "reply"}),
    4: frozenset({"update_ticket"}),
    5: frozenset({"handoff"}),
    6: frozenset({"archive"}),
    8: frozenset({"create_ticket", "handoff", "reply"}),
}


@dataclass(frozen=True)
class Decision:
    ok: bool
    code: str = ""
    message: str = ""
    escalate: bool = False                     # True: thư phải chuyển NEEDS_HUMAN
    data: dict = field(default_factory=dict)


ALLOW = Decision(True)


def deny(code: str, message: str, *, escalate: bool = False) -> Decision:
    return Decision(False, code, message, escalate)


@dataclass
class MessageState:
    task_type: int | None = None
    done: set[str] = field(default_factory=set)
    needs_handoff: bool = False

    def record(self, action: str, *, needs_handoff: bool = False) -> None:
        if action not in WRITE_ACTIONS:
            raise ValueError(f"hành động không hợp lệ: {action}")
        self.done.add(action)
        if needs_handoff:
            self.needs_handoff = True


def check_set_task_type(state: MessageState, task_type: object) -> Decision:
    if isinstance(task_type, bool) or task_type not in TASK_TYPES:
        return deny("invalid_task_type", f"task_type phải thuộc {list(TASK_TYPES)}")
    if state.done:
        return deny("task_type_locked", "Đã có hành động ghi, không đổi loại tác vụ được nữa.")
    return ALLOW


def _common(state: MessageState, mail: ParsedMail, action: str, *, enforce_matrix: bool=True) -> Decision:
    if mail.truncated:
        return deny('needs_human_truncated', 'Thân thư quá dài, bị cắt; cần người xem.', escalate=True)
    if not mail.body_text.strip():
        return deny('needs_human_empty_body', 'Thư không có nội dung đọc được; cần người xem.', escalate=True)
    if state.task_type is None:
        return deny('task_type_required', 'Hãy gọi set_task_type trước khi thực hiện hành động ghi.')
    if isinstance(state.task_type, bool) or state.task_type not in TASK_TYPES:
        return deny('invalid_task_type', 'Loại tác vụ không hợp lệ; hãy khai qua set_task_type.')
    if enforce_matrix and action not in ALLOWED[state.task_type]:
        return deny('action_not_allowed', f'Loại tác vụ {state.task_type} không cho phép {action}.')
    if action in state.done:
        return deny('already_done', f'Đã thực hiện {action} cho thư này rồi.')
    return ALLOW


def check_create_ticket(state: MessageState, mail: ParsedMail, priority: object, *, enforce_matrix: bool=True) -> Decision:
    d = _common(state, mail, 'create_ticket', enforce_matrix=enforce_matrix)
    if not d.ok:
        return d
    if priority not in PRIORITIES:
        return deny('invalid_priority', f'priority phải thuộc {list(PRIORITIES)}')
    return ALLOW


def check_get_ticket(mail: ParsedMail, extracted_ids: list[str], ticket_id: str, ticket: dict | None, *, created_for_message: bool=False, enforce_ownership: bool=True) -> Decision:
    """ID nằm trong email hoặc code xác nhận tạo cho email này; vẫn kiểm ownership."""
    if ticket_id not in extracted_ids and (not created_for_message) or ticket is None or (enforce_ownership and ticket.get('requester') != mail.sender):
        return deny('ticket_not_found', f'Không tìm thấy ticket {ticket_id} cho người gửi này.')
    return ALLOW


def check_update_ticket(state: MessageState, mail: ParsedMail, extracted_ids: list[str], ticket_id: str, ticket: dict | None, status: object, priority: object, *, enforce_ownership: bool=True, enforce_matrix: bool=True) -> Decision:
    d = _common(state, mail, 'update_ticket', enforce_matrix=enforce_matrix)
    if not d.ok:
        return d
    d = check_get_ticket(mail, extracted_ids, ticket_id, ticket, enforce_ownership=enforce_ownership)
    if not d.ok:
        return d
    assert ticket is not None
    if status is None and priority is None:
        return deny('nothing_to_update', 'Cần ít nhất một trong status hoặc priority.')
    if status is not None and status not in STATUSES:
        return deny('invalid_status', f'status phải thuộc {list(STATUSES)}')
    if priority is not None and priority not in PRIORITIES:
        return deny('invalid_priority', f'priority phải thuộc {list(PRIORITIES)}')
    old_status, old_priority = (ticket['status'], ticket['priority'])
    if old_status == 'closed' and status != 'open':
        return deny('closed_can_only_reopen', 'Ticket đã đóng chỉ có thể được mở lại (status=open).')
    new_status = status or old_status
    needs = new_status == 'open' and (old_status != 'open' or (priority is not None and priority != old_priority))
    return Decision(True, data={'needs_handoff': needs})


def check_reply(state: MessageState, mail: ParsedMail, sent_today: int, daily_cap: int, *, enforce_matrix: bool=True) -> Decision:
    d = _common(state, mail, 'reply', enforce_matrix=enforce_matrix)
    if not d.ok:
        return d
    block = reply_block(mail)
    if block is not None:
        return deny(block.code, block.message, escalate=block.kind == 'human')
    if state.needs_handoff and 'handoff' not in state.done:
        return deny('handoff_first', 'Hãy chuyển giao (route handoff) trước khi trả lời, để thư trả lời nói đúng sự thật.')
    if sent_today >= daily_cap:
        return deny('recipient_daily_cap', f'Đã gửi {sent_today} thư cho người này hôm nay (giới hạn {daily_cap}).', escalate=True)
    return Decision(True, data={'recipient': mail.return_path})


def check_route(state: MessageState, mail: ParsedMail, target: object, handoff_address: str | None, *, expected_address: str | None=None, enforce_mapping: bool=True, enforce_matrix: bool=True) -> Decision:
    if target not in ('handoff', 'archive'):
        return deny('invalid_target', "target phải là 'handoff' hoặc 'archive'.")
    d = _common(state, mail, str(target), enforce_matrix=enforce_matrix)
    if not d.ok:
        return d
    if target == 'handoff' and (not handoff_address):
        return deny('no_handoff_target', 'Không xác định được quản trị nhận cho loại nguồn này.', escalate=True)
    if target == 'handoff' and expected_address is not None and enforce_mapping and (handoff_address != expected_address):
        return deny('handoff_target_mismatch', 'Người phụ trách đề xuất không khớp nguồn gửi; dùng auto hoặc đích đúng mapping.')
    return Decision(True, data={'address': handoff_address} if target == 'handoff' else {})


def obligations(state: MessageState, mail: ParsedMail) -> list[str]:
    """Những việc còn phải làm trước khi kết thúc thư (dùng để nhắc model một lần)."""
    if state.task_type is None:
        return ["set_task_type"]
    need = set(REQUIRED[state.task_type])
    if state.needs_handoff:
        need.add("handoff")
    if "reply" in need and reply_block(mail) is not None:
        need.discard("reply")
    return sorted(need - state.done)


def check_append_ticket_note(state: MessageState, mail: ParsedMail, extracted_ids: list[str], ticket_id: str, ticket: dict | None, *, created_for_message: bool=False, enforce_ownership: bool=True, enforce_matrix: bool=True) -> Decision:
    d = _common(state, mail, 'append_ticket_note', enforce_matrix=enforce_matrix)
    if not d.ok:
        return d
    return check_get_ticket(mail, extracted_ids, ticket_id, ticket, created_for_message=created_for_message, enforce_ownership=enforce_ownership)
