from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

VALID_CLASSES = ("client", "partner")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_KB_ID = re.compile(r"^KB-\d{3}$")


class ConfigError(ValueError):
    """Cấu hình thiếu hoặc sai."""


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    thinking: bool
    request_timeout_s: float


@dataclass(frozen=True)
class MailConfig:
    host: str
    imap_port: int
    smtp_port: int
    user: str
    ca_file: Path
    password_env: str


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int
    episode_timeout_s: int
    episode_token_cap: int
    body_limit_chars: int
    kb_top_k: int
    max_run_messages: int
    max_replies_per_recipient_per_day: int


@dataclass(frozen=True)
class PathsConfig:
    db: Path
    kb_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class Config:
    llm: LLMConfig
    mail: MailConfig
    agent: AgentConfig
    paths: PathsConfig
    sender_classes: dict[str, str]
    handoff: dict[str, str]
    kb_always_load: tuple[str, ...]


def _section(raw: dict, name: str) -> dict:
    val = raw.get(name)
    if not isinstance(val, dict):
        raise ConfigError(f"thiếu hoặc sai kiểu mục '{name}' (cần dạng mapping)")
    return val


def _get(sec: dict, key: str, typ: type, where: str, *, positive: bool = False) -> Any:
    if key not in sec:
        raise ConfigError(f"thiếu khóa '{where}.{key}'")
    val = sec[key]
    if typ is bool:
        ok = isinstance(val, bool)
    elif typ is int:
        ok = isinstance(val, int) and not isinstance(val, bool)
    elif typ is float:
        ok = isinstance(val, (int, float)) and not isinstance(val, bool)
    else:
        ok = isinstance(val, typ)
    if not ok:
        raise ConfigError(f"'{where}.{key}' phải là {typ.__name__}, nhận {type(val).__name__}")
    if typ is str and not val.strip():
        raise ConfigError(f"'{where}.{key}' không được rỗng")
    if positive and val <= 0:
        raise ConfigError(f"'{where}.{key}' phải > 0, nhận {val}")
    return float(val) if typ is float else val


def _path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_config(path: str | Path) -> Config:
    """Đọc YAML bằng safe_load, kiểm tra kiểu/giá trị, trả về Config bất biến.

    Đường dẫn tương đối trong file được tính từ thư mục gốc dự án
    (= thư mục cha của thư mục chứa agent.yaml).
    """
    p = Path(path).expanduser().resolve()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"không đọc được {p}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML lỗi trong {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: nội dung gốc phải là mapping")
    base = p.parent.parent

    s = _section(raw, "llm")
    llm = LLMConfig(
        base_url=_get(s, "base_url", str, "llm"),
        api_key=_get(s, "api_key", str, "llm"),
        model=_get(s, "model", str, "llm"),
        thinking=_get(s, "thinking", bool, "llm"),
        request_timeout_s=_get(s, "request_timeout_s", float, "llm", positive=True),
    )

    s = _section(raw, "mail")
    user = _get(s, "user", str, "mail")
    if not _EMAIL.match(user):
        raise ConfigError(f"'mail.user' không phải địa chỉ email hợp lệ: {user!r}")
    mail = MailConfig(
        host=_get(s, "host", str, "mail"),
        imap_port=_get(s, "imap_port", int, "mail", positive=True),
        smtp_port=_get(s, "smtp_port", int, "mail", positive=True),
        user=user,
        ca_file=_path(base, _get(s, "ca_file", str, "mail")),
        password_env=_get(s, "password_env", str, "mail"),
    )

    s = _section(raw, "agent")
    names = ("max_steps", "episode_timeout_s", "episode_token_cap", "body_limit_chars",
             "kb_top_k", "max_run_messages", "max_replies_per_recipient_per_day")
    agent = AgentConfig(**{n: _get(s, n, int, "agent", positive=True) for n in names})

    s = _section(raw, "paths")
    paths = PathsConfig(
        db=_path(base, _get(s, "db", str, "paths")),
        kb_dir=_path(base, _get(s, "kb_dir", str, "paths")),
        log_dir=_path(base, _get(s, "log_dir", str, "paths")),
    )

    sc_raw = _section(raw, "sender_classes")
    sender_classes: dict[str, str] = {}
    for dom, cls in sc_raw.items():
        if not isinstance(dom, str) or dom != dom.lower() or not _DOMAIN.match(dom):
            raise ConfigError(f"'sender_classes': domain không hợp lệ (cần chữ thường): {dom!r}")
        if cls not in VALID_CLASSES:
            raise ConfigError(f"'sender_classes.{dom}': loại phải thuộc {VALID_CLASSES}, nhận {cls!r}")
        sender_classes[dom] = cls
    if not sender_classes:
        raise ConfigError("'sender_classes' không được rỗng")

    ho_raw = _section(raw, "handoff")
    handoff: dict[str, str] = {}
    for cls, addr in ho_raw.items():
        if cls not in VALID_CLASSES:
            raise ConfigError(f"'handoff': loại không hợp lệ {cls!r}")
        if not isinstance(addr, str) or not _EMAIL.match(addr):
            raise ConfigError(f"'handoff.{cls}': địa chỉ email không hợp lệ: {addr!r}")
        handoff[cls] = addr.lower()
    missing = sorted(set(sender_classes.values()) - set(handoff))
    if missing:
        raise ConfigError(f"'handoff' thiếu người nhận cho loại: {missing}")

    kb_sec = _section(raw, "kb")
    al = kb_sec.get("always_load")
    if not isinstance(al, list) or not all(isinstance(x, str) and _KB_ID.match(x) for x in al):
        raise ConfigError("'kb.always_load' phải là danh sách mã dạng KB-123")

    return Config(llm=llm, mail=mail, agent=agent, paths=paths,
                  sender_classes=sender_classes, handoff=handoff,
                  kb_always_load=tuple(al))
