CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,
    project_name    TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    git_remote_url  TEXT,
    git_branch      TEXT NOT NULL DEFAULT 'main',
    webhook_secret  TEXT,
    settings_json   TEXT NOT NULL DEFAULT '{}',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id             TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    department_role    TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    label              TEXT,
    key_prefix         TEXT NOT NULL,
    compound_key_hash  TEXT UNIQUE NOT NULL,
    secret_sealed      TEXT,
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at       DATETIME,
    revoked_at         DATETIME,
    last_sync_log_id   INTEGER NOT NULL DEFAULT 0,
    last_sync_at       DATETIME
);
CREATE INDEX IF NOT EXISTS idx_api_keys_project ON api_keys(project_id);

CREATE TABLE IF NOT EXISTS mutation_logs (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    department       TEXT NOT NULL,
    subagent_role    TEXT,
    target_uri       TEXT NOT NULL,
    compact_summary  TEXT NOT NULL,
    importance       TEXT NOT NULL DEFAULT 'normal',
    previous_etag    TEXT,
    new_etag         TEXT,
    commit_sha       TEXT,
    affects          TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mutation_logs_project_id ON mutation_logs(project_id, log_id);
CREATE INDEX IF NOT EXISTS idx_mutation_logs_project_day ON mutation_logs(project_id, created_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id       TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    key_id           TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    department_role  TEXT NOT NULL,
    transport        TEXT NOT NULL,
    client_info      TEXT,
    connected_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    disconnected_at  DATETIME
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_project ON agent_sessions(project_id, disconnected_at);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id      TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    key_id           TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    department       TEXT NOT NULL,
    target_uri       TEXT NOT NULL,
    mode             TEXT NOT NULL,
    patch_content    TEXT NOT NULL,
    compact_summary  TEXT NOT NULL,
    expected_etag    TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    reviewer_note    TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    decided_at       DATETIME
);
CREATE INDEX IF NOT EXISTS idx_proposals_project_status ON proposals(project_id, status);

CREATE TABLE IF NOT EXISTS documents (
    doc_id           TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    file_name        TEXT NOT NULL,
    original_name    TEXT NOT NULL,
    media_type       TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    text_chars       INTEGER NOT NULL DEFAULT 0,
    content_hash     TEXT NOT NULL,
    access_json      TEXT NOT NULL DEFAULT '["*"]',
    summary          TEXT,
    uploaded_by      TEXT NOT NULL DEFAULT 'admin',
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, created_at);

CREATE TABLE IF NOT EXISTS pending_jobs (
    job_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    job_type         TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    done_at          DATETIME,
    last_error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_jobs_open ON pending_jobs(project_id, done_at, next_attempt_at);
