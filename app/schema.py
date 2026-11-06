"""Database schema, written once and adapted to SQLite or PostgreSQL."""
from __future__ import annotations

SCHEMA_VERSION = 5

PROJECT_TABLES = """CREATE TABLE projects (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE project_sources (
  project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY (project_id, source_id)
);
CREATE TABLE project_reports (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  days INTEGER NOT NULL,
  data TEXT NOT NULL,
  ai_notes TEXT NOT NULL DEFAULT '',
  ai_model TEXT NOT NULL DEFAULT '',
  ai_cost_usd {REAL},
  ai_check TEXT NOT NULL DEFAULT '{{}}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_project_reports ON project_reports(project_id, id);
CREATE INDEX idx_events_org_source_ts ON events(org_id, source_id, ts);
"""

V4_TABLES = """ALTER TABLE project_reports ADD COLUMN shared INTEGER NOT NULL DEFAULT 0;
CREATE TABLE workflow_runs (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  workflow_id BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  workflow_name TEXT NOT NULL,
  conversation_id BIGINT,
  status TEXT NOT NULL DEFAULT 'running',
  steps TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  started_by TEXT NOT NULL,
  finished_by TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_workflow_runs ON workflow_runs(org_id, workflow_id, id);
CREATE TABLE issue_verdicts (
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  issue_key TEXT NOT NULL,
  verdict TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  PRIMARY KEY (org_id, issue_key)
);
"""

# An alert email waiting for its automatic investigation. Kept in the database so a restart can't lose it.
V5_TABLES = """CREATE TABLE held_alerts (
  conversation_id BIGINT PRIMARY KEY,
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  message TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT NOT NULL,
  channel_ids TEXT NOT NULL,
  cleared_at TEXT,
  released INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

COMMON = """
CREATE TABLE settings (
  org_id BIGINT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (org_id, key)
);

CREATE TABLE organizations (
  id {PK},
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  contact_email TEXT NOT NULL DEFAULT '',
  policies TEXT NOT NULL DEFAULT '{{}}',
  created_at TEXT NOT NULL
);

CREATE TABLE users (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  username TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  pending_email TEXT,
  password_hash TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'viewer',
  is_active INTEGER NOT NULL DEFAULT 1,
  is_platform_admin INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  email_verified_at TEXT,
  sso_subject TEXT,
  last_login_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_users_org ON users(org_id);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE email_tokens (
  id {PK},
  purpose TEXT NOT NULL,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE sso_states (
  state_hash TEXT PRIMARY KEY,
  org_id BIGINT NOT NULL,
  nonce TEXT NOT NULL,
  verifier TEXT NOT NULL,
  expires_at TEXT NOT NULL
);


CREATE TABLE sources (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config TEXT NOT NULL DEFAULT '{{}}',
  secret_enc TEXT NOT NULL DEFAULT '',
  poll_seconds INTEGER NOT NULL DEFAULT 15,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_sources_org ON sources(org_id);

CREATE TABLE integrations (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  config TEXT NOT NULL DEFAULT '{{}}',
  secret_enc TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_integrations_org ON integrations(org_id);

CREATE TABLE panels (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  query TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_panels_org ON panels(org_id, source_id);

CREATE TABLE events (
  id {PK},
  org_id BIGINT NOT NULL,
  source_id BIGINT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX idx_events_org_ts ON events(org_id, id);

CREATE TABLE change_events (
  id {PK},
  org_id BIGINT NOT NULL,
  ts TEXT NOT NULL,
  origin TEXT NOT NULL,
  service TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX idx_change_events_org ON change_events(org_id, ts);

CREATE TABLE context_snapshots (
  id {PK},
  org_id BIGINT NOT NULL,
  version BIGINT NOT NULL,
  ts TEXT NOT NULL,
  material_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX idx_snapshots_org ON context_snapshots(org_id, ts);

CREATE TABLE conversations (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL DEFAULT '',
  owner_id BIGINT,
  visibility TEXT NOT NULL DEFAULT 'private',
  shared_by TEXT NOT NULL DEFAULT '',
  shared_at TEXT,
  trigger TEXT NOT NULL DEFAULT 'manual',
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  root_cause_note TEXT NOT NULL DEFAULT '',
  alert_names TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_conversations_org ON conversations(org_id, updated_at);

CREATE TABLE messages (
  id {PK},
  conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'complete',
  error TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  context_version BIGINT,
  context_as_of TEXT,
  notices TEXT NOT NULL DEFAULT '[]',
  verification TEXT NOT NULL DEFAULT '{{}}',
  cost_usd {REAL},
  input_tokens BIGINT,
  output_tokens BIGINT,
  duration_ms BIGINT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE llm_calls (
  id {PK},
  org_id BIGINT NOT NULL,
  conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id BIGINT,
  round INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  snapshot_id BIGINT REFERENCES context_snapshots(id) ON DELETE SET NULL,
  context_version BIGINT,
  request_json TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_text TEXT NOT NULL DEFAULT '',
  response_tool_calls TEXT NOT NULL DEFAULT '[]',
  stop_reason TEXT NOT NULL DEFAULT '',
  input_tokens BIGINT,
  output_tokens BIGINT,
  cost_usd {REAL},
  first_token_ms BIGINT,
  duration_ms BIGINT,
  status TEXT NOT NULL DEFAULT 'running',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX idx_llm_calls_org ON llm_calls(org_id, started_at);
CREATE INDEX idx_llm_calls_conversation ON llm_calls(conversation_id, id);

CREATE TABLE tool_calls (
  id {PK},
  org_id BIGINT NOT NULL,
  conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id BIGINT,
  llm_call_id BIGINT REFERENCES llm_calls(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  arguments TEXT NOT NULL,
  result TEXT NOT NULL,
  ok INTEGER NOT NULL,
  duration_ms BIGINT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_tool_calls_conversation ON tool_calls(conversation_id, id);

CREATE TABLE workflows (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  match_rules TEXT NOT NULL DEFAULT '{{}}',
  risk TEXT NOT NULL DEFAULT 'medium',
  steps TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  origin TEXT NOT NULL DEFAULT 'manual',
  created_by TEXT NOT NULL DEFAULT '',
  reviewed_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_workflows_org ON workflows(org_id);

CREATE TABLE workflow_records (
  id {PK},
  org_id BIGINT NOT NULL,
  workflow_id BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE channels (
  id {PK},
  org_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  min_severity TEXT NOT NULL DEFAULT 'warning',
  config_enc TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_channels_org ON channels(org_id);

CREATE TABLE deliveries (
  id {PK},
  org_id BIGINT NOT NULL,
  channel_id BIGINT,
  fingerprint TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_deliveries_org ON deliveries(org_id, id);

CREATE TABLE audit_log (
  id {PK},
  org_id BIGINT NOT NULL,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX idx_audit_org ON audit_log(org_id, id);
"""

SQLITE_EXTRA = """
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE VIRTUAL TABLE search_index USING fts5(org_id UNINDEXED, conversation_id UNINDEXED, body);
"""



SQLITE_TYPES = {"PK": "INTEGER PRIMARY KEY", "REAL": "REAL"}
POSTGRES_TYPES = {"PK": "BIGSERIAL PRIMARY KEY", "REAL": "DOUBLE PRECISION"}


def _split(body: str) -> list[str]:
    return [item.strip() for item in body.split(";") if item.strip()]


def upgrade_statements(version: int, types: dict[str, str]) -> list[str]:
    """Statements that bring a database from an older version to the current one, keeping its data."""
    statements: list[str] = []
    if version < 3:
        statements += ["DROP TABLE IF EXISTS licenses"] + _split(PROJECT_TABLES.format(**types))
    if version < 4:
        statements += [
            "ALTER TABLE users ADD COLUMN pending_email TEXT",
            "ALTER TABLE conversations ADD COLUMN owner_id BIGINT",
            "ALTER TABLE conversations ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'",
            "ALTER TABLE conversations ADD COLUMN shared_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE conversations ADD COLUMN shared_at TEXT",
            # Ownership moves from a text label to the account itself, so changing an email can't lose or transfer it.
            "UPDATE conversations SET owner_id = (SELECT u.id FROM users u WHERE u.org_id = conversations.org_id "
            "AND (u.email = conversations.created_by OR u.username = conversations.created_by) LIMIT 1)",
            "UPDATE conversations SET visibility = 'team' WHERE trigger = 'auto'",
            "DROP TABLE IF EXISTS eval_runs",
        ] + _split(V4_TABLES.format(**types))
    if version < 5:
        statements += _split(V5_TABLES.format(**types))
    return statements


def sqlite_statements() -> str:
    return COMMON.format(**SQLITE_TYPES) + PROJECT_TABLES.format(**SQLITE_TYPES) + V4_TABLES.format(**SQLITE_TYPES) + V5_TABLES.format(**SQLITE_TYPES) + SQLITE_EXTRA


def postgres_statements() -> list[str]:
    statements = _split(COMMON.format(**POSTGRES_TYPES)) + _split(PROJECT_TABLES.format(**POSTGRES_TYPES)) + _split(V4_TABLES.format(**POSTGRES_TYPES)) + _split(V5_TABLES.format(**POSTGRES_TYPES))
    statements += [
        """CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $fn$
BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;
$fn$ LANGUAGE plpgsql""",
        """CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()""",
        """CREATE TABLE search_index (
  id BIGSERIAL PRIMARY KEY,
  org_id BIGINT NOT NULL,
  conversation_id BIGINT NOT NULL,
  body TEXT NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
)""",
        "CREATE INDEX idx_search_tsv ON search_index USING GIN (tsv)",
        "CREATE INDEX idx_search_org ON search_index(org_id, conversation_id)",
    ]
    return statements
