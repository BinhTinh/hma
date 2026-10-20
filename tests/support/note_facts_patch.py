"""Helpers for note-facts patch/backup regression tests; no CLI entrypoint."""
from __future__ import annotations
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

RULES = '- Tính đúng thời điểm của ghi chú: phân biệt thông tin khách cho biết, hành động hệ thống đã được tool xác nhận và nội dung tham khảo/dự kiến. Với hành động của hệ thống, chỉ ghi là đã thực hiện khi tool tương ứng xác nhận thành công.\n- Nếu ghi trước handoff/reply, không khẳng định đã hướng dẫn, đã trả lời, đã gửi hoặc đã chuyển giao cho hành động chưa được xác nhận. Hướng dẫn từ KB chỉ ghi là "KB-... nêu/đề xuất ..."; không suy ra đã gửi hướng dẫn hoặc khách đã làm theo khi chưa có bằng chứng.\n'
ANCHOR = '- Khi cần ghi, thực hiện sau thao tác tạo/cập nhật ticket phù hợp và trước handoff/reply nếu có.'
MANIFEST_NAME = 'memory_note_facts_patch.json'
EVIDENCE_FILES = ('plan.json','summary.json','evidence.json','attempts.jsonl')


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sha_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def template_node(tree):
    nodes = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'SYSTEM_TEMPLATE' for t in n.targets)]
    if len(nodes) != 1 or not isinstance(nodes[0].value, ast.Constant) or not isinstance(nodes[0].value.value, str):
        raise ValueError('SYSTEM_TEMPLATE không khớp cấu trúc đã đọc')
    return nodes[0]


def build_patch(source):
    before = ast.parse(source)
    text = template_node(before).value.value
    if 'Tính đúng thời điểm của ghi chú:' in text:
        raise ValueError('Quy tắc đã có; không vá lặp. Dùng driver kiểm tra nếu đã áp dụng.')
    if text.count(ANCHOR) != 1 or source.count(ANCHOR) != 1:
        raise ValueError('Điểm chèn trong prompt không khớp bản 3B; chưa ghi đè')
    after_source = source.replace(ANCHOR, RULES + ANCHOR, 1)
    after = ast.parse(after_source)
    new_text = template_node(after).value.value
    if new_text != text.replace(ANCHOR, RULES + ANCHOR, 1):
        raise ValueError('Nội dung template thay đổi ngoài phạm vi')
    first, second = copy.deepcopy(before), copy.deepcopy(after)
    template_node(first).value = ast.Constant(value='TEMPLATE_OMITTED')
    template_node(second).value = ast.Constant(value='TEMPLATE_OMITTED')
    if ast.dump(first, include_attributes=False) != ast.dump(second, include_attributes=False):
        raise ValueError('Có thay đổi cấu trúc mã ngoài template')
    new_text.format(docs='KIỂM_TRA_FORMAT')
    return after_source


def evidence_hashes(folder):
    folder = Path(folder).resolve()
    result = {}
    for name in EVIDENCE_FILES:
        path = folder / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Thiếu bằng chứng harness: {name}')
        result[name] = {'sha256': sha_file(path), 'bytes': path.stat().st_size}
    return result


def atomic_source_write(path, content):
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        shutil.copymode(path, temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_patch(root, evidence_dir, *, write=False):
    root, evidence_dir = Path(root).resolve(), Path(evidence_dir).resolve()
    target = (root / 'hma' / 'prompts.py').resolve()
    if not target.is_relative_to(root):
        raise ValueError('prompts.py nằm ngoài project')
    with target.open('r', encoding='utf-8', newline='') as handle:
        source = handle.read()
    patched = build_patch(source)
    hashes = evidence_hashes(evidence_dir)
    manifest_path = root / MANIFEST_NAME
    preview = {'mode':'check_only', 'changed_agent_files':['hma/prompts.py'],
               'before_sha256':sha_text(source), 'after_sha256':sha_text(patched),
               'evidence_directory':str(evidence_dir), 'evidence_files':list(hashes),
               'api_calls_in_this_step':0, 'automatic_model_reruns':0}
    if not write:
        return preview
    if manifest_path.exists():
        raise FileExistsError('Manifest đã tồn tại; không ghi đè hoặc vá lặp')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = root / '.hma-patches' / ('memory-note-facts-' + stamp + '-' + uuid.uuid4().hex[:6])
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    (backup / 'hma').mkdir()
    (backup / 'harness_evidence').mkdir()
    shutil.copy2(target, backup / 'hma' / 'prompts.py')
    for name in EVIDENCE_FILES:
        shutil.copy2(evidence_dir / name, backup / 'harness_evidence' / name)
    if evidence_hashes(backup / 'harness_evidence') != hashes or evidence_hashes(evidence_dir) != hashes:
        raise ValueError('Bằng chứng thay đổi khi sao lưu; chưa sửa prompt')
    manifest = dict(preview, mode='applied', patch='common_memory_note_facts_v1',
                    backup_directory=str(backup), evidence_hashes=hashes,
                    applied_at_utc=datetime.now(timezone.utc).isoformat(),
                    new_wording_revalidated_with_real_model=False)
    written = False
    try:
        atomic_source_write(target, patched)
        written = True
        if evidence_hashes(evidence_dir) != hashes:
            raise ValueError('Bằng chứng gốc thay đổi; phục hồi prompt')
        fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    except BaseException:
        if written:
            shutil.copy2(backup / 'hma' / 'prompts.py', target)
        raise
    return dict(manifest, manifest_file=str(manifest_path), evidence_preserved=True)


def verify_manifest(root, manifest):
    root = Path(root).resolve()
    if sha_file(root / 'hma' / 'prompts.py') != manifest['after_sha256']:
        raise ValueError('Prompt hiện tại không khớp bản đã vá')
    backup = Path(manifest['backup_directory'])
    if sha_file(backup / 'hma' / 'prompts.py') != manifest['before_sha256']:
        raise ValueError('Backup prompt không khớp')
    for folder in (Path(manifest['evidence_directory']), backup / 'harness_evidence'):
        if evidence_hashes(folder) != manifest['evidence_hashes']:
            raise ValueError('Bằng chứng harness gốc hoặc bản sao không khớp hash')
    return True




