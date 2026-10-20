PRAGMA user_version = 1;

CREATE TABLE messages (
  message_key   TEXT PRIMARY KEY,
  mailbox       TEXT NOT NULL,
  uidvalidity   INTEGER NOT NULL,
  uid           INTEGER NOT NULL,
  sender        TEXT,
  sender_class  TEXT CHECK (sender_class IS NULL OR sender_class IN ('client','partner')),
  subject       TEXT,
  task_type     INTEGER CHECK (task_type IS NULL OR task_type IN (1,2,3,4,5,6,8)),
  status        TEXT NOT NULL CHECK (status IN
                  ('NEW','CLAIMED','DONE','NEEDS_HUMAN','FAILED','SKIPPED')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  handoff_status TEXT NOT NULL DEFAULT 'NONE'
                  CHECK (handoff_status IN ('NONE','SENT','FAILED')),
  reason        TEXT,
  truncated     INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),
  claimed_at    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (mailbox, uidvalidity, uid)
);

CREATE TABLE tickets (
  id                 TEXT PRIMARY KEY CHECK (id GLOB 'TKT-[0-9][0-9][0-9][0-9]'),
  subject            TEXT NOT NULL,
  requester          TEXT NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('open','pending','resolved','closed')),
  priority           TEXT NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
  assignee           TEXT,
  source_message_key TEXT REFERENCES messages(message_key),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE actions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  message_key TEXT NOT NULL REFERENCES messages(message_key),
  action_type TEXT NOT NULL CHECK (action_type IN
                ('create_ticket','update_ticket','reply','handoff','archive')),
  target      TEXT,
  status      TEXT NOT NULL CHECK (status IN ('INTENT','SENT','DONE','FAILED')),
  detail      TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (message_key, action_type)
);

CREATE TABLE llm_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  message_key       TEXT NOT NULL REFERENCES messages(message_key),
  step              INTEGER NOT NULL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  total_tokens      INTEGER,
  reasoning_tokens  INTEGER,
  cache_hit_tokens  INTEGER,
  cache_miss_tokens INTEGER,
  latency_ms        INTEGER,
  finish_reason     TEXT,
  created_at        TEXT NOT NULL
);

CREATE TABLE kb_docs (
  doc_id      TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  always_load INTEGER NOT NULL DEFAULT 0 CHECK (always_load IN (0,1))
);

CREATE VIRTUAL TABLE kb_fts USING fts5(
  doc_id UNINDEXED, title, body,
  tokenize = 'unicode61 remove_diacritics 0'
);
