# AI Computer Brain — Build Plan

**Open-source, self-hosted Company Second Brain MCP Server**

| | |
|---|---|
| Repo name | `company-second-brain-mcp` (product name: **AI Computer Brain**) |
| Purpose of this file | Single source of truth for *what* we build, *why*, and *in what order* |
| Last updated | 2026-10-15 |
| Status | Planning complete. Implementation not started (see §27 Roadmap). |
| Proposed license | MIT |

---

## Table of contents

1. What this is
2. The problem we solve: multi-agent split-brain
3. The solution in one picture
4. Vocabulary
5. End-to-end scenarios (how it will actually work)
6. Architecture and technology choices
7. Repository layout
8. Metadata database
9. On-disk vault layout
10. The six canonical Markdown files
11. Compound API keys
12. Role-based access control (ACCESS_PERMISSIONS.md)
13. Markdown engine (heading tree and ETags)
14. MCP surface (tools, resources, prompts, transports)
15. The agent sync protocol
16. Real-time sync internals
17. Concurrency, locks and conflicts
18. Git sync and GitHub webhook
19. COMPANY_LOG.md and archiving
20. Database schema sync
21. Admin dashboard (/admin)
22. CLI (`brain`)
23. Deployment (local PC, VPS, Cloudflare, PostgreSQL)
24. Connecting agents (Hermes, OpenClaw, Claude Code, Serena, cloud agents)
25. Security checklist
26. Testing strategy
27. Implementation roadmap
28. Decisions log
29. Open questions
30. Not in scope for v1
31. Master prompt for Claude (generate the repository)

---

## 1. What this is

An open-source, self-hosted **control plane for AI agents**. You install it once on a VPS or a local
PC, the same way you install Hermes or OpenClaw. You open the admin dashboard (or use the CLI),
create a **project**, and generate a **compound API key** for every agent that belongs to that
project. Each agent, wherever it runs, points its MCP client at the server with its key.

From then on every agent holding a key for the same **project ID** shares one living knowledge base:
six canonical, Git-backed Markdown files. When one agent changes something, every other agent in that
project learns about it on its next turn, for a few dozen tokens.

Positioning: this is the **brain layer of an agent operating system**. It is not an agent runtime,
not a chat UI and not a vector database. It sits beside Hermes, OpenClaw, Claude Code and custom
agents and tells them what is true right now.

**No LLM key is required.** The server never calls a language model. Each agent brings its own model
(Hermes, OpenClaw, Claude Code and cloud agents already have one). To connect, a user needs exactly
two values: the server URL and a compound API key.

---

## 2. The problem we solve: multi-agent split-brain

A typical company fleet today:

| Agent | Where it runs | What it owns |
|---|---|---|
| Sales Hermes | VPS 1 | CRM, pricing conversations, deal terms |
| Marketing Hermes | VPS 2 | Landing pages, campaigns, collateral |
| OpenClaw bot | Home server | Messaging, support, scheduling |
| Claude Code + Serena | Developer laptop | Codebase, migrations, infra |
| Custom cloud agent | AWS / Railway | Analytics, reporting |

Without a shared synchronization layer these agents fail in four ways:

1. **State desynchronization.** Sales changes a price or a deal term. Marketing and Finance keep
   operating on yesterday's assumptions because nothing tells them.
2. **Brand and design drift.** The web/UI agent changes colour tokens or typography. Content agents
   keep producing assets with the old rules.
3. **SOP and prompt fragmentation.** A core workflow changes. Engineers must hunt down and edit system
   prompts in five repositories and server configs.
4. **Token bloat.** The naive fix (paste the whole company manual, schema and logs into every system
   prompt) burns most of the context window on text that did not change.

---

## 3. The solution in one picture

```
                    brain.yourdomain.com (VPS)   or   http://localhost:8000 (local PC)
                 ┌──────────────────────────────────────────────────────────────────┐
 Sales Hermes    │  ┌────────┐   ┌──────────────────────┐   ┌─────────────────────┐ │
 (VPS 1)  ──────►│  │ Caddy  │──►│ FastAPI + FastMCP    │──►│ brain_files/        │ │
 Marketing       │  │ HTTPS  │   │  /mcp  (Streamable)  │   │  acme-corp/   (git) │ │
 Hermes ────────►│  │ no SSE │   │  /sse  (legacy SSE)  │   │   DESIGN_SYSTEM.md  │ │
 (VPS 2)         │  │ buffer │   │  /events (plain SSE) │   │   TECH_AND_INFRA.md │ │
 OpenClaw ──────►│  └────────┘   │  /admin  (Tailwind)  │   │   DB_SCHEMA_STATE.md│ │
 (home server)   │               │  /api/admin/*        │   │   COMPANY_LOG.md    │ │
 Claude Code ───►│               │  /webhooks/github    │   │   ACCESS_PERMISSIONS│ │
 (laptop)        │               └─────────┬────────────┘   │   COMPANY_PULSE.md  │ │
 Cloud agent ───►│                         │                │  beta-fintech/ (git)│ │
 (AWS/Railway)   │        ┌────────────────┼──────────────┐ └─────────────────────┘ │
                 │        ▼                ▼              ▼                          │
                 │  ┌───────────┐   ┌────────────┐  ┌───────────┐                   │
                 │  │ SQLite or │   │ Redis      │  │ Git       │                   │
                 │  │ Postgres  │   │ pub/sub    │  │ remote    │                   │
                 │  │ metadata  │   │ (optional) │  │ (optional)│                   │
                 │  └───────────┘   └────────────┘  └───────────┘                   │
                 └──────────────────────────────────────────────────────────────────┘
```

One server. Many isolated projects. Each project has its own vault, its own keys, its own sync room.

---

## 4. Vocabulary

| Term | Meaning |
|---|---|
| **Control plane** | The single server install (VPS or PC) that hosts every project. |
| **Project / workspace** | An isolated tenant identified by a `project_id` such as `acme-corp`. One company, one client, or one product. |
| **Vault** | The directory `brain_files/{project_id}/`, a Git repository holding the six canonical files. |
| **Canonical files** | `DESIGN_SYSTEM.md`, `TECH_AND_INFRA.md`, `DB_SCHEMA_STATE.md`, `COMPANY_LOG.md`, `ACCESS_PERMISSIONS.md`, `COMPANY_PULSE.md`. |
| **Section** | The text under one `##` or `###` heading in a canonical file. The unit of reading, writing, hashing and locking. |
| **ETag** | First 8 hex chars of SHA-256 over a section's normalized body. Changes when the section changes. |
| **Compound API key** | `sb_<project_id>_<role>_<agent_id>_<secret>`. Binds one agent to one project and one role. |
| **Department role** | `admin`, `engineering`, `sales`, `marketing`, `ops`, `design` by default. Drives RBAC. |
| **Agent ID** | A stable name for one agent instance, e.g. `hermes-sales-01`. |
| **Sub-agent role** | Optional label a parent agent passes when a spawned sub-agent writes, e.g. `ui-subagent`. |
| **Sync room** | The set of live connections (MCP sessions and SSE streams) belonging to one project. Events are broadcast per room. |
| **Mutation ledger** | The `mutation_logs` table plus its mirror in `COMPANY_LOG.md`. Every change, who made it, one-line summary. |
| **Hot log** | The last 50 ledger entries kept in `COMPANY_LOG.md`. Older entries move to `logs/archive-YYYY-MM.md`. |

---

## 5. End-to-end scenarios (how it will actually work)

### 5.1 Install the server

Local PC (zero config, SQLite, no Redis):

```bash
pipx install company-second-brain-mcp      # or: uvx company-second-brain-mcp
brain init                                 # writes .env with a generated ADMIN_MASTER_KEY
brain serve                                # http://localhost:8000/admin
```

VPS (HTTPS, Caddy, optional Redis):

```bash
git clone https://github.com/<org>/company-second-brain-mcp && cd company-second-brain-mcp
cp .env.example .env     # set BRAIN_DOMAIN=brain.yourdomain.com and ADMIN_MASTER_KEY
docker compose up -d     # https://brain.yourdomain.com/admin
```

A one-line `curl -fsSL https://.../install.sh | bash` wraps the above, mirroring the Hermes and
OpenClaw install experience.

### 5.2 Create a project and keys

In `/admin` (or `brain project create acme-corp --name "Acme Corp"`):

1. The server creates `brain_files/acme-corp/`, runs `git init`, copies the six template files,
   makes the first commit.
2. A row is inserted into `projects`.
3. The admin clicks **Create Agent Key**, picks role `sales`, types agent ID `hermes-sales-01`.
4. The server generates `sb_acme-corp_sales_hermes-sales-01_<43-char secret>`, stores only its SHA-256
   hash, and shows the plaintext exactly once.
5. Repeat for `marketing/hermes-marketing-01`, `ops/openclaw-home`, `engineering/claude-code-01`,
   `engineering/analytics-cloud-01`.

### 5.3 Connect the agents

Each agent gets two things: the server URL and its key. No cloning, no file sync.

- Sales Hermes on VPS 1: `~/.hermes/config.yaml` → MCP server `https://brain.yourdomain.com/mcp` with
  header `Authorization: Bearer sb_acme-corp_sales_...`.
- Marketing Hermes on VPS 2: same URL, its own marketing key.
- OpenClaw at home: same URL via mcporter, its ops key.
- Claude Code on the laptop: `claude mcp add --transport http company-brain <url> --header ...`.
- Cloud agent on AWS: the `mcp` Python SDK Streamable HTTP client with the header.

When each connects, the auth layer hashes the key, finds the row, confirms the parsed
`project_id/role/agent_id` match the row, and attaches the principal to the session. The session
joins the `acme-corp` sync room. Nothing from `beta-fintech` is ever visible to it.

### 5.4 A change propagates (the core loop)

1. Sales Hermes closes a deal that changes pricing. It calls
   `update_brain_section(file_name="COMPANY_PULSE.md", heading="Pricing Tiers",
   patch_content="...", compact_summary="Pro tier $39→$49/mo; annual discount 20%",
   expected_etag="a1b2c3d4")`.
2. Principal is already resolved: project `acme-corp`, role `sales`, agent `hermes-sales-01`.
3. RBAC loads `ACCESS_PERMISSIONS.md` (cached by its ETag). `sales` has `rw` on `COMPANY_PULSE.md`.
4. The server takes the per-heading lock `acme-corp / COMPANY_PULSE.md / pricing-tiers`.
5. The Markdown engine parses the file, locates the heading, checks `expected_etag` equals the
   current ETag (otherwise returns `CONFLICT` with the current body), replaces the body, recomputes
   ETags, writes the file atomically.
6. A `mutation_logs` row is inserted (`log_id` 1287). A one-line entry is prepended to the hot log in
   `COMPANY_LOG.md`; entry 51, if any, moves to `logs/archive-2026-10.md`.
7. A Git commit is queued: `[sales/hermes-sales-01] Pro tier $39→$49/mo; annual discount 20%`.
   If the project has a remote configured, it is pushed in the background.
8. The hub publishes `{type: section_updated, project_id, file, heading, etag, agent_id, department,
   summary, log_id, ts}` to the `acme-corp` room: MCP `notifications/resources/updated` to every live
   MCP session, and a JSON event to every `/events` SSE stream.
9. The tool returns `{status: "ok", new_etag: "e5f6a7b8", log_id: 1287}`.
10. Marketing Hermes, at the start of its next turn, calls `get_company_log_delta(since_entry_id=1286)`
    and receives one ~20-token entry. It calls
    `read_brain_section("COMPANY_PULSE.md", "Pricing Tiers", known_etag="a1b2c3d4")` and gets only
    the eight changed lines. It regenerates the landing page with the right price.
11. The engineering key can read that section but a write attempt returns `FORBIDDEN`.

### 5.5 Hermes sub-agents writing concurrently

A Hermes instance spawns two sub-agents. Both pass `subagent_role` in their calls.

- One writes `DESIGN_SYSTEM.md ## Colors`, the other `DESIGN_SYSTEM.md ## Typography`. Different
  heading locks, so both proceed in parallel. The log shows
  `design/hermes-ui-01[color-subagent]` and `design/hermes-ui-01[type-subagent]`.
- Both target `## Colors`. The second waits for the lock, then its `expected_etag` is stale, so it
  receives `CONFLICT` with the fresh body, merges at the agent level and retries. No lost update.

### 5.6 A human pushes to GitHub

1. GitHub sends a signed `push` payload to `/webhooks/{project_id}/github`.
2. The server verifies the HMAC, extracts commits, authors and changed paths.
3. One hot-log entry per commit: `[human/github] alice: "Add Stripe webhook handler" (3 files)`.
4. If the push touched vault files (a project whose remote is the same repo), the server runs
   `git pull --rebase`, recomputes ETags and broadcasts updates for every changed section.
5. Optional path mapping (Phase 3): changes under `infra/` or `Dockerfile` append a note to
   `TECH_AND_INFRA.md ## Recent Changes`.

### 5.7 The database schema changes

- Push mode: Claude Code runs a migration and calls `sync_db_schema(schema_payload, compact_summary)`.
  The server rewrites the affected table sections of `DB_SCHEMA_STATE.md`, logs the change, broadcasts.
- Watch mode: `brain db-watch --dsn postgresql://... --project acme-corp --key sb_...` runs beside the
  company database, introspects `information_schema` on an interval, and calls the same tool when it
  sees a diff. Analytics agents stop hallucinating column names.

### 5.8 An agency hosting several clients

One VPS, projects `acme-corp`, `beta-fintech`, `gamma-shop`. Each has its own vault, Git history,
keys, hot log and sync room. The admin switches between them in `/admin`. Isolation is enforced by
the key's project ID on every request, never by what the agent claims in a tool argument.

### 5.9 Revoking a compromised key

Admin clicks **Revoke** (or `brain key revoke <key_id>`). `revoked_at` is set. The next request with
that key gets `401`. Live sessions using it are closed within one heartbeat. No restart.

---

## 6. Architecture and technology choices

| Layer | Choice | Why |
|---|---|---|
| Language / runtime | Python 3.11+ | Same ecosystem as Hermes and the MCP Python SDK; easy for contributors. |
| Web framework | FastAPI on Starlette, served by Uvicorn | Async, typed, ASGI mountable alongside the MCP app. |
| MCP server | `FastMCP` from the official `mcp` SDK (fallback: `fastmcp` 2.x) | Tools, resources, prompts, Streamable HTTP and SSE out of the box; mounts into Starlette. |
| Metadata DB | SQLAlchemy 2 async. `aiosqlite` by default, `asyncpg` when `DATABASE_URL` is PostgreSQL | Zero-config locally; production-grade on a VPS with one env-var change. |
| Schema init | `sql/01_init_metadata.sql` applied on startup (dialect-aware); Alembic from v0.3 | Matches the agreed spec; migrations later without redesign. |
| Pub/sub | In-process asyncio hub by default; Redis 7 when `REDIS_URL` is set | A local install needs no Redis. Multi-worker VPS installs get cross-process fan-out. |
| Markdown | Hand-written line-based heading parser that respects fenced code blocks | Predictable, round-trips files byte-for-byte outside the edited section, no heavy AST dependency. |
| Hashing | SHA-256; 8-hex ETags; SHA-256 of full key for credentials | Fast, deterministic, no salt needed for 32-byte random secrets. |
| Git | `git` binary via `asyncio.create_subprocess_exec` | No GitPython dependency; identical behaviour in Docker and on bare metal. |
| Admin UI | Single `admin_dashboard.html`, Tailwind CSS (CDN), Alpine.js, served by FastAPI | No frontend build step. One file to read and customise. |
| CLI | Typer | `brain serve|init|project|key|db-watch` with help text for free. |
| Reverse proxy | Caddy 2 | Automatic HTTPS; `flush_interval -1` keeps SSE unbuffered. |
| Containers | Docker Compose: `brain-server`, `redis`, `caddy` | Turnkey VPS deployment. |

### Request lifecycle

```
HTTP request
  → Caddy (TLS, no buffering)
  → Starlette middleware: parse key → SHA-256 → lookup → principal (project, role, agent, key_id)
       (no key → 401; revoked → 401; path /admin,/api/admin → ADMIN_MASTER_KEY instead)
  → principal stored in a contextvar + request.state
  → FastMCP tool handler (reads principal from contextvar; project_id argument must match)
  → RBAC check against ACCESS_PERMISSIONS.md
  → per-heading lock → markdown_engine → atomic file write
  → mutation_logs insert + COMPANY_LOG.md append
  → git_sync queue (debounced commit, optional push)
  → hub.publish(project_id, event) → MCP notifications + /events SSE
  → response
```

---

## 7. Repository layout

Agreed file names are kept. The package is nested under `src/second_brain/` so the project is
pip/pipx installable with a `brain` console script.

```
company-second-brain-mcp/
├── README.md                     # Install, concepts, agent connection snippets
├── plan.md                       # This document
├── LICENSE                       # MIT
├── pyproject.toml                # package metadata, console script `brain`
├── requirements.txt              # pinned runtime deps (mirrors pyproject)
├── .env.example
├── Dockerfile
├── docker-compose.yml            # VPS: brain-server + redis + caddy
├── docker-compose.local.yml      # PC: brain-server only, SQLite, no TLS
├── Caddyfile
├── scripts/
│   └── install.sh                # one-liner installer (pipx or docker path)
├── sql/
│   └── 01_init_metadata.sql
├── src/second_brain/
│   ├── __init__.py
│   ├── config.py                 # pydantic-settings: env vars, paths, feature flags
│   ├── database.py               # async engine/session, startup schema apply, dialect shims
│   ├── models.py                 # SQLAlchemy models: Project, ApiKey, MutationLog, AgentSession
│   ├── auth.py                   # key generation, parsing, hashing, verification, middleware
│   ├── rbac.py                   # ACCESS_PERMISSIONS.md parser + permission checks
│   ├── markdown_engine.py        # heading tree, ETags, surgical section replace/append
│   ├── locks.py                  # per-heading asyncio locks (+ Redis lock when multi-worker)
│   ├── hub.py                    # sync rooms: in-process broadcaster, Redis pub/sub adapter
│   ├── git_sync.py               # commit queue, push, webhook verification + ingestion
│   ├── company_log.py            # hot-log writer, archiving, delta queries
│   ├── vault.py                  # project provisioning from templates, path safety
│   ├── server.py                 # FastMCP app: tools, resources, prompts; ASGI composition
│   ├── web_ui.py                 # /admin page + /api/admin/* JSON endpoints + /events
│   ├── webhooks.py               # /webhooks/{project_id}/github
│   ├── db_watch.py               # schema introspection client used by `brain db-watch`
│   └── cli.py                    # Typer CLI
├── templates/
│   ├── default_vault/
│   │   ├── DESIGN_SYSTEM.md
│   │   ├── TECH_AND_INFRA.md
│   │   ├── DB_SCHEMA_STATE.md
│   │   ├── COMPANY_LOG.md
│   │   ├── ACCESS_PERMISSIONS.md
│   │   └── COMPANY_PULSE.md
│   └── admin_dashboard.html
├── docs/
│   ├── connecting-agents.md      # long-form version of README snippets
│   ├── deployment-vps.md
│   ├── cloudflare-domain.md      # DNS, TLS modes, Tunnel, Access, custom domains on Railway/Render
│   ├── admin-panel-hosting.md    # same-process default; split static hosting on Pages, Vercel, Railway
│   └── architecture.md
└── tests/
    ├── test_markdown_engine.py
    ├── test_auth.py
    ├── test_rbac.py
    ├── test_tools.py             # MCP tools end-to-end against a temp vault
    ├── test_hub.py
    ├── test_git_sync.py
    └── test_admin_api.py
```

---

## 8. Metadata database

Where it lives:

- **Local PC**: `./data/brain_metadata.db` (SQLite). Created automatically on first run.
- **VPS, default**: SQLite inside the `brain_data` Docker volume. No extra container.
- **VPS, PostgreSQL**: install PostgreSQL natively (`apt install postgresql`) or as a container, set
  `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/second_brain`, restart. Same schema, applied
  automatically.

The user never runs SQL by hand. `database.py` applies `sql/01_init_metadata.sql` on startup with
`CREATE TABLE IF NOT EXISTS` and small dialect shims (`AUTOINCREMENT` vs `GENERATED ... AS IDENTITY`,
`DATETIME` vs `TIMESTAMPTZ`).

```sql
-- 1. PROJECTS (multi-tenant workspaces)
CREATE TABLE IF NOT EXISTS projects (
    project_id      TEXT PRIMARY KEY,                 -- 'acme-corp'  [a-z0-9-]{3,40}
    project_name    TEXT NOT NULL,
    storage_path    TEXT NOT NULL,                    -- absolute path to brain_files/{project_id}/
    git_remote_url  TEXT,                             -- optional push target
    git_branch      TEXT NOT NULL DEFAULT 'main',
    webhook_secret  TEXT,                             -- HMAC secret for /webhooks/{project_id}/github
    settings_json   TEXT NOT NULL DEFAULT '{}',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. API KEYS (role-scoped compound credentials)
CREATE TABLE IF NOT EXISTS api_keys (
    key_id             TEXT PRIMARY KEY,              -- random 12-char id, shown in UI
    project_id         TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    department_role    TEXT NOT NULL,                 -- 'admin','engineering','sales','marketing','ops','design'
    agent_id           TEXT NOT NULL,                 -- 'hermes-sales-01'
    label              TEXT,                          -- free text, e.g. 'Sales Hermes on VPS 1'
    key_prefix         TEXT NOT NULL,                 -- 'sb_acme-corp_sales_hermes-sales-01_a1b2' for display
    compound_key_hash  TEXT UNIQUE NOT NULL,          -- SHA-256 hex of the full plaintext key
    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at       DATETIME,
    revoked_at         DATETIME,
    last_sync_log_id   INTEGER NOT NULL DEFAULT 0,  -- latest log_id fetched via get_company_log_delta (drives the sync banner)
    last_sync_at       DATETIME
);
CREATE INDEX IF NOT EXISTS idx_api_keys_project ON api_keys(project_id);

-- 3. MUTATION LEDGER (cross-agent audit trail; source of truth for deltas)
CREATE TABLE IF NOT EXISTS mutation_logs (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL,                   -- 'agent_update','admin_edit','human_commit','db_schema','system'
    agent_id         TEXT NOT NULL,                   -- or 'github', 'admin-ui', 'system'
    department       TEXT NOT NULL,
    subagent_role    TEXT,
    target_uri       TEXT NOT NULL,                   -- 'brain://acme-corp/COMPANY_PULSE.md#pricing-tiers'
    compact_summary  TEXT NOT NULL,                   -- <= 160 chars
    importance       TEXT NOT NULL DEFAULT 'normal', -- 'low','normal','high'; set by the writer, drives relevance filtering
    previous_etag    TEXT,
    new_etag         TEXT,
    commit_sha       TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mutation_logs_project_id ON mutation_logs(project_id, log_id);

-- 4. AGENT SESSIONS (live connections shown in the admin UI)
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id       TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    key_id           TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    department_role  TEXT NOT NULL,
    transport        TEXT NOT NULL,                   -- 'mcp-http','mcp-sse','events-sse'
    client_info      TEXT,                            -- MCP client name/version if provided
    connected_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    disconnected_at  DATETIME
);

-- 5. PROPOSALS (writes awaiting human approval under the rw-approve permission)
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id      TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    key_id           TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    department       TEXT NOT NULL,
    target_uri       TEXT NOT NULL,
    mode             TEXT NOT NULL,                   -- 'replace','append','create','delete'
    patch_content    TEXT NOT NULL,
    compact_summary  TEXT NOT NULL,
    expected_etag    TEXT,
    status           TEXT NOT NULL DEFAULT 'pending', -- 'pending','approved','rejected','superseded'
    reviewer_note    TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    decided_at       DATETIME
);

-- 6. PENDING JOBS (durable outbox for Git commits, pushes and broadcasts; replayed on startup)
CREATE TABLE IF NOT EXISTS pending_jobs (
    job_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    job_type         TEXT NOT NULL,                   -- 'git_commit','git_push','broadcast','webhook_pull'
    payload_json     TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    done_at          DATETIME,
    last_error       TEXT
);
```

---

## 9. On-disk vault layout

```
brain_files/                       # BRAIN_FILES_DIR, bind-mounted in Docker
└── acme-corp/                     # one Git repository per project
    ├── .git/
    ├── DESIGN_SYSTEM.md
    ├── TECH_AND_INFRA.md
    ├── DB_SCHEMA_STATE.md
    ├── COMPANY_LOG.md             # hot log, last 50 entries
    ├── ACCESS_PERMISSIONS.md
    ├── COMPANY_PULSE.md
    ├── logs/
    │   └── archive-2026-10.md     # entries that aged out of the hot log
    └── .brain/
        └── project.json           # project_id, template_version, created_at
```

Path safety: file names are validated against the fixed allow-list of six names (plus `logs/*` for
reads). No user-supplied path ever reaches the filesystem unchecked.

---

## 10. The six canonical Markdown files

| File | Holds | Default `##` headings in template | Typical writers |
|---|---|---|---|
| `DESIGN_SYSTEM.md` | Brand colour tokens, typography scale, spacing, component patterns, copy tone of voice | Brand Colors, Typography, Spacing & Layout, Components, Tone of Voice, Do & Don't | design, marketing |
| `TECH_AND_INFRA.md` | Languages, framework versions, services, API endpoints, VPS/container map, env conventions | Stack, Services & Endpoints, Deployments, Environments & Secrets Policy, Recent Changes | engineering |
| `DB_SCHEMA_STATE.md` | Engines, one section per table with columns and types, migration log | Engines, Tables (one `###` per table), Migration Log | engineering, `sync_db_schema`, db-watch |
| `COMPANY_LOG.md` | Hot activity stream, last 50 mutations from agents, humans (GitHub) and schema changes | Hot Log, How To Read This File | system only |
| `ACCESS_PERMISSIONS.md` | RBAC matrix: role × file → `r`/`rw`/`-`, plus per-heading overrides | Roles, File Matrix, Heading Overrides, Agent Rules, File Guide, Sync Policy, Notes | admin only |
| `COMPANY_PULSE.md` | Strategy, pricing tiers, shipped features, current sprint priorities, open decisions | Mission & Positioning, Pricing Tiers, Shipped Recently, Current Priorities, Open Decisions | sales, marketing, ops, admin |

Template rules:

- `# Title` on line 1, a one-paragraph purpose statement, then `##` sections. Agents may add new `##`
  or `###` headings; the server never deletes a heading unless an admin does.
- Every template ends with a hidden-to-humans but useful `## Agent Notes` section describing what
  belongs in the file and what does not, so agents self-correct.

---

## 11. Compound API keys

Format (agreed): `sb_<project_id>_<role>_<agent_id>_<secret>`

Example: `sb_acme-corp_sales_hermes-sales-01_Qm3k9vXz2LpR8tYw4NbC7fHd1JgS6aEu0ZxVoKiM5nT`

Rules that make the format unambiguous and safe:

- `project_id`, `role`, `agent_id` match `^[a-z0-9][a-z0-9-]*$` (no underscores), lengths 3–40.
  Splitting on `_` therefore yields exactly five parts.
- `secret` is 32 random bytes from `secrets.token_bytes`, base62 encoded (43 chars).
- Stored: `compound_key_hash = sha256(full_plaintext_key)`, `key_prefix` for display. Plaintext is
  shown once in the UI/CLI and never logged.
- Verification: parse → hash → single indexed lookup → confirm the row's `project_id`, `role`,
  `agent_id` equal the parsed values → `revoked_at IS NULL` → update `last_used_at` (throttled).
  Comparison uses `hmac.compare_digest`.
- Transport, in order of preference: `Authorization: Bearer <key>`; `X-Brain-Key: <key>`;
  `?key=<key>` query parameter for clients that cannot set headers (documented as last resort,
  stripped from access logs).
- The **admin master key** (`ADMIN_MASTER_KEY` env var, generated by `brain init`) is separate. It
  unlocks `/admin` and `/api/admin/*` and can call any MCP tool for any project by passing
  `project_id` explicitly. Agent keys ignore or must match the `project_id` argument.
- Rotation: generate a new key, update the agent, revoke the old one. No downtime.

---

## 12. Role-based access control (ACCESS_PERMISSIONS.md)

The file is both human-readable documentation and the machine-read policy.

```markdown
## Roles
admin, engineering, sales, marketing, ops, design

## File Matrix
| File                  | admin | engineering | sales | marketing | ops | design |
|-----------------------|-------|-------------|-------|-----------|-----|--------|
| DESIGN_SYSTEM.md      | rw    | r           | r     | rw        | r   | rw     |
| TECH_AND_INFRA.md     | rw    | rw          | r     | r         | rw  | r      |
| DB_SCHEMA_STATE.md    | rw    | rw          | r     | r         | r   | r      |
| COMPANY_LOG.md        | r     | r           | r     | r         | r   | r      |
| ACCESS_PERMISSIONS.md | rw    | r           | r     | r         | r   | r      |
| COMPANY_PULSE.md      | rw    | r           | rw    | rw        | rw  | r      |

## Heading Overrides
| Target                               | Role      | Permission |
|--------------------------------------|-----------|------------|
| COMPANY_PULSE.md#pricing-tiers       | marketing | r          |
| TECH_AND_INFRA.md#recent-changes     | sales     | rw         |

## Sync Policy
| Role        | Watch                                                                    | Cadence    |
|-------------|--------------------------------------------------------------------------|------------|
| sales       | COMPANY_PULSE.md, DESIGN_SYSTEM.md#tone-of-voice                         | every-turn |
| marketing   | COMPANY_PULSE.md, DESIGN_SYSTEM.md                                       | every-turn |
| engineering | TECH_AND_INFRA.md, DB_SCHEMA_STATE.md, COMPANY_PULSE.md#current-priorities | every-turn |

## Agent Rules
- The brain is the summary, not the database: verify live details in the source system before acting.
- Log every customer-facing price change in both pricing-tiers and open-decisions.

## File Guide
(Seeded from the table in §15.1. Edit freely; served to agents through whoami.)
```

Enforcement:

- `rbac.py` parses the two tables, caches the result keyed by the file's ETag, and re-parses only
  when the file changes.
- Heading overrides win over the file matrix. `admin` always has `rw` everywhere except
  `COMPANY_LOG.md`, which only the system writes.
- A fourth permission value, `rw-approve`, turns writes into proposals. The server validates the
  write exactly as for `rw`, stores it in `proposals` with the diff, returns
  `PENDING_APPROVAL {proposal_id}` to the agent, and lists it in the admin Approvals panel. On
  approval the stored patch is re-checked against the current ETag, then applied, logged, committed
  and broadcast with `event_type = 'approved_update'`; on rejection the agent sees `REJECTED` with
  the reviewer's note via `get_proposal`. The decision is deterministic policy, not a model. The
  template ships with no `rw-approve` entries; the README shows how to protect
  `COMPANY_PULSE.md#pricing-tiers`.
- A missing or unparseable matrix fails closed: everything read-only except for `admin`.
- Writes to `ACCESS_PERMISSIONS.md` itself require `admin`. Roles not listed under `## Roles` cannot
  be issued keys (the UI dropdown is populated from this section per project).

---

## 13. Markdown engine (heading tree and ETags)

- Parse with a single pass over lines. Track fenced code blocks (``` and ~~~) so `##` inside code is
  never treated as a heading. Recognise ATX headings `#`–`###`; deeper levels are plain body text.
- Build a tree: file → `##` sections → `###` subsections. Each node knows its heading text, GitHub
  style slug, start line, end line, body.
- A `##` section's body includes its nested `###` subsections. Reading a `###` returns only that
  subsection. Updating a `###` replaces text up to the next heading of the same or higher level.
- ETag per node: `sha256(normalized_body)[:8]`, where normalization is LF line endings and trailing
  whitespace stripped per line. File ETag: `sha256("\n".join(section_etags))[:8]`.
- Heading lookup accepts the exact heading text or its slug, case-insensitively.
- Write operations: `replace` (default), `append` (add lines at the end of the section body),
  `create` (add a new `##`/`###` at the end of the file or parent section when the heading does not
  exist), `delete` (admin only). Everything outside the target range is written back byte-for-byte.
- Writes are atomic: write to `file.md.tmp` in the same directory, `fsync`, `os.replace`.
- Frontmatter (`---` block at top), the `# Title` and the purpose paragraph are preserved untouched.

---

## 14. MCP surface (tools, resources, prompts, transports)

Transports:

- `POST/GET /mcp` — Streamable HTTP (current MCP spec, primary).
- `GET /sse` + `POST /messages` — legacy HTTP+SSE for older clients.
- `GET /events` — plain Server-Sent Events JSON stream for custom agents that are not MCP clients.
  Same key auth, same room events, heartbeat comment every 20 s.

Tools (project is always taken from the key; `project_id` argument is optional and only honoured for
the admin master key):

| Tool | Arguments | Returns | Notes |
|---|---|---|---|
| `whoami` | — | `{project_id, role, agent_id, permissions: {file: "r"/"rw"}, latest_log_id, rules: [...], file_guide: {...}}` | Agents call this once at session start. Rules and file guide are role-filtered (§15.1). |
| `get_brain_index` | `project_id?` | For each of the six files: file ETag plus every `##`/`###` heading with its ETag. ~120–200 tokens. | The table of contents. |
| `read_brain_section` | `file_name, heading, known_etag?` | `NOT_MODIFIED` if ETag matches, else `{etag, content}` with only that section's lines. | The 95 % token saver. |
| `read_brain_file` | `file_name, known_etag?` | Whole file or `NOT_MODIFIED`. | For first load or small files. |
| `update_brain_section` | `file_name, heading, patch_content, compact_summary, mode="replace"/"append"/"create", expected_etag?, subagent_role?, importance?` | `{status: ok, new_etag, log_id}` or `CONFLICT {current_etag, current_content}` or `FORBIDDEN` or `PENDING_APPROVAL {proposal_id}` (§12). | RBAC, lock, write, log, commit, broadcast. `compact_summary` ≤ 160 chars, required. |
| `get_company_log_delta` | `since_entry_id=0, limit=50, include_low=false` | `{entries: [{id, ts, actor, dept, target, summary, etag}], latest_id}` | ~15–20 tokens per entry. Zero entries when nothing happened. |
| `sync_db_schema` | `schema_payload (JSON: engine, tables[{name, columns[{name,type,nullable,default}], indexes}]), compact_summary` | `{status, changed_tables, new_etag, log_id}` | Rewrites only the affected `### table` sections of `DB_SCHEMA_STATE.md`. Requires `rw` on that file. |
| `list_projects` | — | Project list | Admin master key only. |
| `get_proposal` | `proposal_id` | `{status: pending/approved/rejected, applied_etag?, reviewer_note?}` | For writes that returned `PENDING_APPROVAL` (§12). |

**Role-filtered tool list.** `tools/list` returns only the tools the caller can use: `sync_db_schema`
only to roles with `rw` on `DB_SCHEMA_STATE.md`, `list_projects` only to the admin master key,
`get_proposal` only when the project has an `rw-approve` entry. Fewer schemas in the agent's context,
fewer wrong picks. `notifications/tools/list_changed` is sent when a permission change alters the set.

Resources:

- `brain://{project_id}/{FILE}.md` — full file, `text/markdown`.
- `brain://{project_id}/{FILE}.md#{heading-slug}` — one section.
- `brain://{project_id}/index` — same payload as `get_brain_index`, `application/json`.
- Server sends `notifications/resources/updated` on every mutation and `resources/list_changed` when
  headings are created or deleted.

Prompts:

- `brain_bootstrap` — returns the agent sync protocol (§15) as a system-prompt-ready text, tailored
  to the caller's role and permissions. Lets Hermes and other clients auto-load the operating rules.

---

## 15. The agent sync protocol

What every connected agent is instructed to do. Shipped as the `brain_bootstrap` prompt and in the
README.

1. **Session start:** call `whoami`, then `get_brain_index`. Read the sections relevant to your role
   with `read_brain_section` and remember each `etag`. Remember `latest_id` from
   `get_company_log_delta(0, 1)`.
2. **Every turn, before doing work:** call `get_company_log_delta(since_entry_id=<latest_id>)`. If
   any entry targets a section you rely on, re-read it with `known_etag`. Update `latest_id`.
3. **After changing something in the real world** (price, deploy, schema, brand token, decision):
   call `update_brain_section` with a one-line `compact_summary` written for other agents, and pass
   `expected_etag` from your last read. On `CONFLICT`, re-read, merge, retry once.
4. **Never** paste whole files into your context when a section will do. Never write secrets into the
   brain.
5. If your client supports MCP notifications, treat `resources/updated` as a hint to run step 2 now.

Polling in step 2 costs a few tokens when nothing changed and makes the system work even with MCP
clients that ignore server notifications.

### 15.1 How the rules reach the agent through MCP (no manual prompt setup)

The agent's operator configures only the URL and the key. The operating rules travel inside the MCP
connection itself, through channels the agent's LLM always sees:

| Channel | When the LLM sees it | What we put there |
|---|---|---|
| MCP `initialize` → `instructions` | On connect. Claude Code, Claude Desktop, Cursor and most clients inject it into the system prompt | The compact sync protocol (§15) in about 120 tokens |
| Tool descriptions (`tools/list`) | Every client sends these to the model | Written as operating instructions: *when* to call and *what* to pass, not only the signature |
| `whoami` response | First call of every session | Role, permissions, `latest_log_id`, the rules again, and the **file guide** below filtered to the role |
| Sync banner on every tool result | Every call | One line such as `[brain] 3 changes since your last sync (latest_id 1290). Call get_company_log_delta.` Omitted when nothing is new |
| `## Agent Notes` in each file | Whenever a file or section is read | What belongs in this file and what does not |
| MCP prompt `brain_bootstrap` | Clients that load server prompts | Full protocol with examples; operators of clients that ignore `instructions` paste it into the Hermes or OpenClaw system prompt once |

Tool descriptions, written as rules:

- `whoami`: "Call this first in every new session. Returns your role, what you may read and write, and the rules for keeping the brain current."
- `get_company_log_delta`: "Call at the start of every turn before doing work, with the latest_id you saw last time. Returns what other agents and humans changed since. About 20 tokens per entry, 0 when nothing changed."
- `read_brain_section`: "Read one heading. Always pass known_etag from your last read; NOT_MODIFIED costs almost nothing. Prefer this over read_brain_file."
- `update_brain_section`: "Call immediately after you change anything other agents must know: a price, a shipped feature, a schema, a brand token, an endpoint, a decision. compact_summary (max 160 chars) is written for the other agents. Pass expected_etag from your last read; on CONFLICT re-read, merge, retry once."
- `sync_db_schema`: "Call after running a migration, or whenever you notice the live database schema differs from DB_SCHEMA_STATE.md."

The file guide (what to update, and where). Served by `whoami`, filtered to what the role may write:

| When the agent has just... | It updates |
|---|---|
| Changed a price, plan, discount or packaging | `COMPANY_PULSE.md#pricing-tiers` |
| Shipped or launched a feature | `COMPANY_PULSE.md#shipped-recently` |
| Changed sprint focus or priorities | `COMPANY_PULSE.md#current-priorities` |
| Made or reversed a business decision | `COMPANY_PULSE.md#open-decisions` |
| Changed a colour, font, spacing or component rule | `DESIGN_SYSTEM.md#brand-colors`, `#typography` or `#components` |
| Changed tone-of-voice or copy rules | `DESIGN_SYSTEM.md#tone-of-voice` |
| Added, moved or removed a service, endpoint or environment | `TECH_AND_INFRA.md#services-endpoints` or `#deployments` |
| Upgraded a framework or runtime | `TECH_AND_INFRA.md#stack` |
| Ran a migration or altered a table | `sync_db_schema`, which rewrites `DB_SCHEMA_STATE.md` |
| Changed who may edit what | `ACCESS_PERMISSIONS.md` (admin only) |
| Anything else other agents should know | `COMPANY_PULSE.md#open-decisions` with a clear summary |

Server-side guardrails that make automatic writes safe: RBAC on every call, a required compact
summary, a 64 KB patch cap, ETag conflict detection, the secret-pattern guard, and `COMPANY_LOG.md`
written only by the server.

What this guarantees and what it does not: a capable model that receives these rules at connect time
and is reminded by the banner on every response reads and writes the brain without any human pasting
prompts. The server still cannot force a call. The admin UI therefore shows each key's last sync time
and flags stale agents (§21). The unread count behind the banner comes from `api_keys.last_sync_log_id`,
updated on every `get_company_log_delta` call.

**Rules are editable per project.** The protocol text and the file guide are not hard-coded. They
live in the vault as `## Agent Rules`, `## File Guide` and `## Sync Policy` sections of
`ACCESS_PERMISSIONS.md` (admin-writable only), seeded from the template. The server reads them when it
builds `instructions`, the `whoami` response and `brain_bootstrap`, so a company can add rules such
as "log every customer-facing price change in both pricing-tiers and open-decisions" from the admin
UI without touching code. Changes take effect on the agent's next `whoami` or reconnect.

**The brain is the summary, not the database.** A default Agent Rule states it: the brain holds the
current truth and pointers (what "the migration" is, which service owns billing, the live price).
Agents verify live details in the source system (the repo, the database, the CRM) before acting on
them, and never copy bulk data into the brain.

**Relevance-filtered deltas.** `## Sync Policy` gives each role a watch list of files and headings
(default: everything the role may read). Every mutation carries an `importance` of `low`, `normal` or
`high`, set by the writer. `get_company_log_delta` and the sync banner count only entries that match
the caller's watch list, and `low` entries are omitted unless `include_low=true`. Marketing is not
woken by a schema edit; nobody is woken by a typo fix.

**Inline inbox.** When the caller has three or fewer unread relevant entries, every tool result
carries the entries themselves after the banner, not just a count. The agent is current without an
extra call. With more than three, the banner asks for a `get_company_log_delta` call.

---

## 16. Real-time sync internals

- `hub.py` exposes `publish(project_id, event)` and `subscribe(project_id) -> async iterator`.
- **Single process (default):** an in-memory dict `project_id -> set[asyncio.Queue]`. Zero
  dependencies. Suitable for local PCs and single-worker VPS installs.
- **Multi-worker (`REDIS_URL` set):** each worker subscribes to Redis channel
  `brain:{project_id}:events` for every project with live sessions, and republishes into its local
  queues. `publish` writes to Redis. Redis `appendonly yes` is on so no event is lost on restart,
  although the ledger, not Redis, is the durable record.
- Event schema: `{type, project_id, file, heading, slug, etag, previous_etag, agent_id, department,
  subagent_role, summary, log_id, ts}`. `type` ∈ `section_updated | section_created |
  section_deleted | file_pulled | schema_synced | human_commit`.
- Fan-out targets per room: MCP sessions (notifications), `/events` streams (JSON lines), admin UI
  activity panel (same `/events` endpoint with the master key).
- Heartbeats every 20 s on all SSE streams to survive proxies, including Cloudflare's 100 s idle cut.

---

## 17. Concurrency, locks and conflicts

- `locks.py` holds `asyncio.Lock` objects keyed by `(project_id, file_name, slug)` plus one per file
  for the physical write. Locks are created lazily and pruned when idle.
- With `REDIS_URL` set and more than one Uvicorn worker, the same key is also taken as a Redis lock
  (`SET NX PX 5000`) so two workers cannot write the same section at once. Default deployment is one
  worker, which is plenty for text edits.
- Optimistic concurrency via `expected_etag` is the primary protection against lost updates across
  agents and sub-agents. Locks only serialize the physical write.
- `CONFLICT` responses include the current body so the agent can merge without an extra round trip.
- Git operations for one project run on a single background task per project, so commits never
  interleave.

---

## 18. Git sync and GitHub webhook

Auto-commit:

- Every mutation enqueues `(project_id, files, message, author)` on the project's Git worker.
- The queue is the `pending_jobs` table, a durable outbox. The mutation and its job rows are written
  in one transaction, the worker marks a job done only after `git commit` succeeds, and unfinished
  jobs are replayed on startup. Pushes and broadcasts use the same table, so a crash never loses a
  commit or an event.
- The worker debounces for 2 s so a burst of sub-agent writes becomes one commit per file set, then
  runs `git add` + `git commit --author="<dept>/<agent_id> <agent_id>@brain.local"`.
- Message format: `[<dept>/<agent_id>] <compact_summary>`; the body lists target URIs and ETags.
- If `git_remote_url` is set the worker runs `git push` with retry and backoff; failures are logged
  to `mutation_logs` as `system` events and shown in the admin UI, never thrown back to the agent.
- Credentials: HTTPS token in the remote URL stored in `settings_json` (encrypted at rest with a key
  derived from `ADMIN_MASTER_KEY`), or an SSH deploy key mounted into the container.

Inbound webhook `/webhooks/{project_id}/github`:

- Verify `X-Hub-Signature-256` with the project's `webhook_secret`. Reject otherwise.
- Handle `push`: one hot-log entry per commit (`[human/github] <author>: "<message>" (<n> files)`).
- If the webhook's repository equals the project's `git_remote_url`, run `git pull --rebase`; on
  conflict the server copy wins, the remote change is logged as `system: webhook conflict on <file>`,
  and the file is force-pushed back. Then recompute ETags and broadcast per changed section.
- Phase 3: configurable path → section mapping to append notes to `TECH_AND_INFRA.md`.

---

## 19. COMPANY_LOG.md and archiving

Hot-log entry format (one line each, newest first, under `## Hot Log`):

```
- #1287 2026-10-15T09:42Z sales/hermes-sales-01 → COMPANY_PULSE.md#pricing-tiers — Pro tier $39→$49/mo; annual discount 20% (a1b2c3d4→e5f6a7b8)
- #1286 2026-10-15T09:10Z human/github alice → repo — "Add Stripe webhook handler" (3 files)
- #1285 2026-10-15T08:55Z engineering/claude-code-01[db-watch] → DB_SCHEMA_STATE.md#orders — Added orders.refund_reason (text, nullable)
```

- `company_log.py` keeps exactly 50 lines in the hot log. Overflow lines are appended to
  `logs/archive-YYYY-MM.md` for the month of the entry.
- `mutation_logs` remains the queryable source; `get_company_log_delta` reads the table, not the file.
- Agents never write `COMPANY_LOG.md` directly; the tool layer does.

---

## 20. Database schema sync

- Tool `sync_db_schema` accepts a normalized JSON payload and diffs it against the `### <table>`
  sections currently in `DB_SCHEMA_STATE.md`. Only changed tables are rewritten; a line is added to
  `## Migration Log`.
- `brain db-watch` (in `db_watch.py`) is a thin client: connects to PostgreSQL (`asyncpg`) or MySQL
  (`aiomysql`, Phase 3) read-only, introspects `information_schema` every N seconds, hashes the
  result, and calls `sync_db_schema` over MCP when the hash changes. Runs anywhere the database is
  reachable, authenticates with an `engineering` key.
- Payload is capped (e.g. 300 tables) and sensitive columns can be masked by name pattern.

---

## 21. Admin dashboard (/admin)

Single file `templates/admin_dashboard.html`, Tailwind CSS via CDN, Alpine.js for state, served by
`web_ui.py`. All data comes from `/api/admin/*` JSON endpoints authenticated with the master key
(entered once, stored in `sessionStorage`).

Hosting: by default the panel is served from the same process, so it is live wherever the server runs
(`/admin` on the VPS domain, the Railway URL or localhost). It also works as a static file hosted on
another origin, pointed at the server through an API base URL setting and allowed by `CORS_ORIGINS`.
Full guidance, including Cloudflare DNS, Tunnel and Access steps, is in §23.

Panels:

1. **Login** — paste `ADMIN_MASTER_KEY`.
2. **Projects** — list with counts (keys, live agents, last mutation); **Create Project** form
   (`project_id`, name, optional Git remote). Delete with typed confirmation.
3. **Project → Keys** — table of keys (prefix, role, agent, label, created, last used, status);
   **Create Agent Key** form (role dropdown from the project's `ACCESS_PERMISSIONS.md`, agent ID,
   label); one-time plaintext reveal with copy button and ready-made config snippets for Hermes,
   OpenClaw, Claude Code; **Revoke**.
4. **Project → Files** — six-file sidebar; viewer renders Markdown with per-section ETags; editor
   edits one section or the whole file; save sends `If-Match: <etag>` and surfaces conflicts.
5. **Project → Activity** — live table fed by `/events` (type, time, actor, target, summary, ETag),
   filters by file and actor, link to the Git commit, and an inline diff of that commit.
6. **Project → Agents** — live sessions from `agent_sessions` (agent, role, transport, connected
   since, last seen, last sync via `get_company_log_delta`, and a **stale** flag when no sync for a
   configurable window, default 30 minutes).
7. **Project → Settings** — Git remote URL and branch, webhook secret with the webhook URL to paste
   into GitHub, archive now, export vault as zip.
8. **First-run setup wizard** — shown automatically when no project exists: confirm or generate the
   master key, create the first project, issue the first agent key, show the connection snippets.
   Gives the same five-minute path as one-click hosted bots.
9. **Project → Rules** — edit the `Agent Rules`, `File Guide` and `Sync Policy` sections that the
   server serves to agents through `instructions`, `whoami` and `brain_bootstrap` (stored in the
   vault, see §15.1).
10. **Project → Approvals** — queue of pending proposals from `rw-approve` writes (§12): who proposed,
    target, summary, diff, Approve or Reject with an optional note. Approving applies, logs, commits
    and broadcasts the change.

Admin API endpoints (all under `/api/admin`, master key required):
`GET/POST /projects`, `DELETE /projects/{id}`, `GET/POST /projects/{id}/keys`,
`DELETE /projects/{id}/keys/{key_id}`, `GET /projects/{id}/files`, `GET/PUT /projects/{id}/files/{name}`,
`GET /projects/{id}/log?since=`, `GET /projects/{id}/sessions`, `PUT /projects/{id}/settings`,
`GET /healthz` (public).

---

## 22. CLI (`brain`)

Everything the UI does, from the terminal, so a headless VPS install needs no browser:

```
brain init                                  # create .env, data dir, generate ADMIN_MASTER_KEY
brain serve [--host --port --workers --tunnel]  # run the server; --tunnel opens a Cloudflare quick tunnel (public HTTPS URL)
brain project create <id> --name "..."      # provision vault + git init
brain project list | delete <id>
brain key create --project <id> --role sales --agent hermes-sales-01 [--label ...]
brain key list --project <id> | revoke <key_id>
brain vault export <id> --out acme.zip | import <id> acme.zip
brain log tail --project <id>               # follow /events in the terminal
brain db-watch --dsn <url> --project <id> --key <sb_...> [--interval 60]
brain doctor                                # check git, db, redis, disk, caddy reachability
```

---

## 23. Deployment (local PC, VPS, Cloudflare, PostgreSQL)

### Local PC

- `pipx install company-second-brain-mcp && brain init && brain serve`, or
  `docker compose -f docker-compose.local.yml up`.
- SQLite at `./data/brain_metadata.db`, vaults at `./brain_files/`, no Redis, no TLS.
- Agents on the same machine use `http://localhost:8000/mcp`. Agents elsewhere on the LAN use the
  PC's IP; for remote agents expose it with a Cloudflare Tunnel or move to the VPS layout.

### VPS with Docker Compose (agreed files)

`docker-compose.yml` services: `brain-server` (build `.`, env `DATABASE_URL`, `REDIS_URL`,
`ADMIN_MASTER_KEY`, volumes `brain_data:/data` and `./brain_files:/app/brain_files`), `redis`
(`redis:7-alpine`, `--appendonly yes`), `caddy` (`caddy:2-alpine`, ports 80/443, `Caddyfile`
mounted read-only). Named volumes `brain_data`, `redis_data`, `caddy_data`, `caddy_config`; network
`brain-net`.

`Caddyfile`:

```
{$BRAIN_DOMAIN} {
    reverse_proxy brain-server:8000 {
        flush_interval -1
        transport http {
            read_timeout 24h
            write_timeout 24h
        }
    }
}
```

### Cloudflare subdomain `brain.yourdomain.com`

- **Option A (simplest):** DNS A record to the VPS IP, proxy status *DNS only* (grey cloud). Caddy
  obtains the Let's Encrypt certificate itself.
- **Option B (proxied, orange cloud):** set SSL mode *Full (strict)*, use a Cloudflare Origin
  Certificate in Caddy or Caddy's Cloudflare DNS challenge. Heartbeats every 20 s keep SSE alive under
  Cloudflare's 100 s idle timeout.
- **Option C (no open ports):** `cloudflared` tunnel to `brain-server:8000`; drop the Caddy service.

### Where the admin panel lives, and how to connect your Cloudflare domain

**Default: the admin panel ships inside the server.** `templates/admin_dashboard.html` is served by the
same Python process at `/admin`, next to `/mcp`. On a VPS it is therefore at
`https://brain.yourdomain.com/admin`, on Railway at `https://<app>.up.railway.app/admin`, on a PC at
`http://localhost:8000/admin`. There is nothing separate to deploy, and this is the recommended setup.
It is protected by the master key; for a second layer, restrict it by IP in the `Caddyfile` or put
Cloudflare Access in front of the `/admin` and `/api/admin` paths (free for up to 50 users).

```
# Caddyfile snippet: allow /admin only from your office or VPN range
@admin path /admin* /api/admin*
handle @admin {
    @allowed remote_ip 203.0.113.0/24
    handle @allowed { reverse_proxy brain-server:8000 }
    respond 403
}
```

**Optional: host the panel somewhere else.** The dashboard is one static file that only calls
`/api/admin/*` and `/events`, so it can live on Cloudflare Pages, Vercel, Netlify, GitHub Pages or a
Railway static service at, say, `https://admin.yourdomain.com`, while the server stays on the VPS.
Requirements, all small:

| On the server | In the static panel |
|---|---|
| `CORS_ORIGINS=https://admin.yourdomain.com` in `.env` | An API base URL field on the login screen, remembered per browser, or a `<meta name="brain-api-base">` tag set at build time |
| `/api/admin/*` and `/events` answer CORS preflight for that origin | The master key is typed by the admin and sent as a bearer token; it is never embedded in the static build |
| Nothing else; MCP endpoints are unaffected | SSE activity stream connects to the API base, not to its own origin |

When this is worth doing: you want the panel on its own domain, behind Cloudflare Access or an SSO
provider, while agents keep hitting `/mcp` directly. What is not possible: hosting the panel without a
running server somewhere, because the panel has no backend of its own.

**Connecting `brain.yourdomain.com` through Cloudflare (VPS path), step by step**

1. Add your domain to Cloudflare and point the registrar's nameservers at Cloudflare, if not done yet.
2. DNS → Records → Add: type `A`, name `brain`, IPv4 = VPS public IP, Proxy status **DNS only** (grey
   cloud), TTL Auto. Add an `AAAA` record too if the VPS has IPv6.
3. On the VPS open ports 80 and 443 (`ufw allow 80,443/tcp`). Caddy needs 80 for the certificate
   challenge and 443 for traffic.
4. In `.env` set `BRAIN_DOMAIN=brain.yourdomain.com` and `PUBLIC_URL=https://brain.yourdomain.com`.
5. `docker compose up -d`. Caddy obtains a Let's Encrypt certificate within a minute; check with
   `docker compose logs caddy`.
6. Verify: `curl -I https://brain.yourdomain.com/healthz` returns `200`; open `/admin` in a browser.
7. Give agents `https://brain.yourdomain.com/mcp` plus their keys. GitHub webhooks go to
   `https://brain.yourdomain.com/webhooks/<project_id>/github`.

Optional variants:

- **Proxied (orange cloud).** Set SSL/TLS mode to *Full (strict)*. Either install a Cloudflare Origin
  Certificate in Caddy (`tls /certs/origin.pem /certs/origin.key`) or build Caddy with the Cloudflare
  DNS module and a scoped API token for the DNS challenge. Add a Cache Rule that bypasses cache for the
  hostname. The server's 20-second heartbeats keep SSE alive under Cloudflare's 100-second idle limit.
- **No open ports (Cloudflare Tunnel).** Zero Trust → Networks → Tunnels → create `brain`, public
  hostname `brain.yourdomain.com` → service `http://brain-server:8000`. Run the `cloudflared` container
  from the compose file with the tunnel token and drop the Caddy service and the 80/443 port mappings.
  TLS terminates at Cloudflare; the certificate is automatic.
- **Admin login layer (Cloudflare Access).** Zero Trust → Access → Applications → Self-hosted, domain
  `brain.yourdomain.com`, path `admin` (and a second one for `api/admin`), policy: allow your email
  addresses. Agents calling `/mcp` are outside the policy and unaffected.
- **Separate admin subdomain (split hosting).** Deploy `admin_dashboard.html` to Cloudflare Pages, then
  DNS → `CNAME admin → <project>.pages.dev`, proxied. Set `CORS_ORIGINS` on the server as above.
- **One domain, many projects.** Projects do not need their own subdomains; the key selects the project.
  A vanity `CNAME acme.brain → brain.yourdomain.com` works if a client wants their own address, since
  the server ignores the hostname.

**Custom domain on Railway or Render.** In the platform dashboard add `brain.yourdomain.com` as a custom
domain; it shows a CNAME target. In Cloudflare add `CNAME brain → <that target>`, DNS only until the
platform has issued its certificate, then optionally proxied with *Full (strict)*. The admin panel is
then at `https://brain.yourdomain.com/admin` exactly as on a VPS.

### One-click deploy buttons, and what Cloudflare can and cannot do

The goal is the same five-minute path hosted Slack bots offer: click a button, enter one or two values,
get a URL. What a self-hoster must supply for our server is small:

| Credential or value | Required? | Notes |
|---|---|---|
| `ADMIN_MASTER_KEY` | Yes | Templates generate it automatically; the setup wizard shows it once |
| Public URL or domain | Yes | Assigned by the platform, or your own domain via Cloudflare DNS |
| GitHub token or deploy key for vault push | Only on hosts with ephemeral disk | Vaults are restored from the Git remote on restart |
| `DATABASE_URL` for PostgreSQL | Optional | SQLite on a persistent volume is the default |
| LLM or memory-service API key | Never | The server is model-free (§1) |

Cloudflare specifically:

- **Deploy to Cloudflare button** (`https://deploy.workers.cloudflare.com/?url=<repo>`): Workers
  applications only. It provisions KV, D1, R2, Hyperdrive, Vectorize, Durable Objects, Queues and
  Workers AI, and collects secrets from `.env.example`. It does **not** provision Containers, and a
  Worker cannot run our Python, FastMCP, SQLite-on-disk and `git` stack. A Workers-native version would
  be a TypeScript rewrite on Durable Objects, D1 and R2, which contradicts the single-codebase decision.
  Not planned.
- **Cloudflare Containers** (Workers Paid plan): can run our Docker image behind a Durable Object that
  routes requests and holds the sync room. Container disk must be treated as ephemeral, so vaults would
  live in the Git remote and metadata in D1 or an external PostgreSQL. Feasible as a separate deploy
  target, but not covered by the Deploy button today. Evaluate in Phase 4.
- **Cloudflare for the edge, not the compute** (recommended now): DNS and TLS in front of any host, or a
  Cloudflare Tunnel to a VPS or home PC with no open ports (option C above). The free plan is enough.
  `brain serve --tunnel` wraps a `cloudflared` quick tunnel so a local PC gets a public HTTPS URL in
  seconds for remote agents.

Docker-native hosts that give the true one-click experience for our image with persistent volumes
(Phase 4 templates): **Railway** (template button), **Render** (`render.yaml` blueprint), **Fly.io**
(`fly.toml`), **DigitalOcean App Platform** (deploy button), and self-hosted panels such as Coolify or
Dokploy. Each asks only for `ADMIN_MASTER_KEY`, or generates it, and returns the URL to paste into agents.

### PostgreSQL on the VPS

- Native: `sudo apt install postgresql`, create role and database, set
  `DATABASE_URL=postgresql+asyncpg://brain:***@host.docker.internal:5432/second_brain` (or the
  host's private IP), restart `brain-server`. Schema applies automatically.
- Container: add a `postgres:16` service to the compose file (documented variant in
  `docs/deployment-vps.md`).

### `.env.example`

```
BRAIN_HOST=0.0.0.0
BRAIN_PORT=8000
PUBLIC_URL=https://brain.yourdomain.com
BRAIN_DOMAIN=brain.yourdomain.com
ADMIN_MASTER_KEY=change-me-brain-init-generates-this
DATABASE_URL=sqlite+aiosqlite:////data/brain_metadata.db
REDIS_URL=                       # empty = in-process hub
BRAIN_FILES_DIR=/app/brain_files
GIT_AUTHOR_NAME=Company Second Brain
GIT_AUTHOR_EMAIL=brain@localhost
GIT_PUSH_ENABLED=true
LOG_LEVEL=info
CORS_ORIGINS=
```

### Backups

- Vaults are Git repos: configure a remote and every change is already off-box.
- Metadata: nightly `sqlite3 .backup` or `pg_dump` via a documented cron line; volumes are named so
  `docker run --rm -v brain_data:/data ... tar` works.

---

## 24. Connecting agents

All snippets use the same two values: the URL and the key. Exact config schemas for third-party
agents must be verified against the version in use when the README is written.

### What a user needs, and what they do not

| Needed | Not needed |
|---|---|
| Server URL: `https://brain.yourdomain.com/mcp` or `http://localhost:8000/mcp` | Any LLM API key, provider or model name on the server |
| One compound API key per agent, issued in `/admin` or with `brain key create` | Cloning the vault or installing anything from this repo on the agent's machine |
| An MCP-capable agent, or any HTTP client for the `/events` stream | Shared passwords or per-file credentials |

The agent's own LLM configuration (provider, model, key) stays where it already is, inside Hermes,
OpenClaw or Claude Code. The brain only sees tool calls, never prompts or model traffic.

**Hermes** (`~/.hermes/config.yaml`, one entry per Hermes instance with its own key):

```yaml
mcp_servers:
  company-brain:
    url: https://brain.yourdomain.com/mcp
    headers:
      Authorization: "Bearer sb_acme-corp_sales_hermes-sales-01_<secret>"
```

**OpenClaw** (via mcporter):

```bash
mcporter config add company-brain \
  --url https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_ops_openclaw-home_<secret>"
```

**Claude Code**:

```bash
claude mcp add --transport http company-brain https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_engineering_claude-code-01_<secret>"
```

**Serena**: Serena is itself an MCP tool server, so it does not consume other MCP servers. Add the
brain to the same host (Claude Code, Cursor, etc.) that runs Serena; both are then available in one
session.

**Cursor / Windsurf / any `mcp.json` client**:

```json
{ "mcpServers": { "company-brain": {
    "url": "https://brain.yourdomain.com/mcp",
    "headers": { "Authorization": "Bearer sb_acme-corp_marketing_hermes-marketing-01_<secret>" } } } }
```

**Custom Python cloud agent**:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

headers = {"Authorization": "Bearer sb_acme-corp_engineering_analytics-cloud-01_<secret>"}
async with streamablehttp_client("https://brain.yourdomain.com/mcp", headers=headers) as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        delta = await session.call_tool("get_company_log_delta", {"since_entry_id": 1286})
```

**Non-MCP agents**: `GET https://brain.yourdomain.com/events` with the same header for a JSON SSE
stream, and the `/api/v1` REST mirror of the read tools (Phase 3).

**Any agent host works, not only the ones above.** The brain does not care where an agent lives.
Anything that can call an MCP server, or at minimum make HTTPS requests, can hold a key and join a
project: Slack, Discord or Telegram bots (including supermemory's open-source company-brain Slack bot,
which calls MCP tool servers and could join with an `ops` key), workflow tools such as n8n or Zapier
MCP, agent frameworks such as LangGraph, CrewAI, AutoGen, the OpenAI Agents SDK or the Vercel AI SDK,
and any custom service in any language. An agent running inside a Slack bot syncs exactly like a
Hermes instance on a VPS: same URL, its own key, same rules delivered in-band.

---

## 25. Security checklist

- Keys hashed with SHA-256; plaintext shown once; never logged; query-param keys scrubbed from logs.
- Project isolation enforced from the key, never from tool arguments. Admin master key is the only
  cross-project credential.
- File name allow-list; no path traversal possible; vault directories created with `0700`.
- RBAC fails closed. `ACCESS_PERMISSIONS.md` and `COMPANY_LOG.md` protected as described.
- Webhook HMAC verification mandatory; replay window of 5 minutes using `X-GitHub-Delivery`.
- Rate limiting per key (default 120 writes/min, 600 reads/min) with `429`.
- `patch_content` size cap (default 64 KB) and `compact_summary` cap (160 chars).
- Secrets never belong in the brain; templates say so; a simple regex guard warns on obvious API
  keys in `patch_content`.
- HTTPS by default on VPS via Caddy; HSTS on; CORS closed unless `CORS_ORIGINS` set.
- Docker image runs as non-root; `git` and `redis` pinned.
- Dependency pinning and `pip-audit` in CI.
- Safe defaults for self-hosters: Git push off until a remote is set, webhooks off until a secret is
  set, approvals inactive until an `rw-approve` entry exists, no tunnel unless `--tunnel` is passed.

---

## 26. Testing strategy

- **Unit:** markdown engine round-trips (edit one section, rest byte-identical; code fences with `##`
  ignored; ETag stability), key parse/generate/verify incl. malformed keys, RBAC matrix and
  overrides, hot-log rotation.
- **Integration (pytest + httpx + temp dirs):** provision project → create key → MCP client calls
  every tool → assert files, ledger rows, Git commits and hub events.
- **Concurrency:** 20 parallel `update_brain_section` calls on the same heading with and without
  `expected_etag`; assert no lost update and correct `CONFLICT` count.
- **Transport:** Streamable HTTP and legacy SSE both pass the same tool test suite.
- **Admin API:** auth required, CRUD, `If-Match` conflict path.
- **Webhook:** valid and invalid signatures, pull-with-conflict path.
- **Smoke in Docker:** `docker compose up` in CI, `brain doctor`, one end-to-end mutation.

---

## 27. Implementation roadmap

Each phase ends with a tagged release and a green test suite.

### Phase 0 — Scaffold (v0.0.x)
- [ ] `pyproject.toml`, `src/second_brain/`, `config.py`, `database.py`, `models.py`, `sql/01_init_metadata.sql`
- [ ] Six template files with agreed headings and `## Agent Notes`
- [ ] `vault.py` provisioning + `git init` + first commit
- [ ] `cli.py`: `init`, `serve`, `project create|list`, `key create|list|revoke`
- [ ] CI: lint (ruff), type check (mypy), tests
- Acceptance: `brain init && brain project create demo && brain key create ...` works on Windows, macOS, Linux with SQLite.

### Phase 1 — Core brain (v0.1, MVP)
- [ ] `markdown_engine.py` with tests
- [ ] `auth.py` middleware + principal contextvar
- [ ] `rbac.py`
- [ ] `locks.py`, in-process `hub.py`
- [ ] `server.py`: `whoami`, `get_brain_index`, `read_brain_section`, `read_brain_file`, `update_brain_section`, `get_company_log_delta`; resources; `brain_bootstrap` prompt; `/mcp` and `/sse`
- [ ] Rules delivered in-band (§15.1): MCP `instructions`, rule-style tool descriptions, role-filtered file guide and rules in `whoami`, sync banner on every tool result, `last_sync_log_id` tracking per key
- [ ] Role-filtered `tools/list` and the inline inbox (up to 3 unread relevant entries in every tool result)
- [ ] `company_log.py` hot log + archive
- [ ] `/events` SSE endpoint
- [ ] `docker-compose.local.yml`, `Dockerfile`
- Acceptance: two MCP clients with different keys on the same project; a write by one is visible to the other via log delta and `read_brain_section` with ETags; a forbidden write returns `FORBIDDEN`; a stale write returns `CONFLICT`.

### Phase 2 — Admin UI, Git, VPS (v0.2)
- [ ] `web_ui.py` + `admin_dashboard.html`: all ten panels, including the first-run setup wizard, the Rules editor and the Approvals queue
- [ ] Agents panel: last sync time per key and a stale flag (default 30 min without a log-delta call)
- [ ] First-run setup wizard at `/admin` (generate master key if unset, first project, first key, connection snippets)
- [ ] Per-project editable `Agent Rules`, `File Guide` and `Sync Policy` in `ACCESS_PERMISSIONS.md` with a Rules panel (§15.1)
- [ ] Durable outbox (`pending_jobs`) for commits, pushes and broadcasts, replayed on startup
- [ ] Inline diff per activity entry, read from the Git commit
- [ ] `brain serve --tunnel` (Cloudflare quick tunnel) and safe defaults (§25)
- [ ] Admin UI split hosting: API base URL setting, `CORS_ORIGINS` preflight on `/api/admin/*` and `/events`, Cloudflare Pages guide (§23)
- [ ] `docs/cloudflare-domain.md` and `docs/admin-panel-hosting.md`
- [ ] `git_sync.py` auto-commit, debounced, optional push
- [ ] `docker-compose.yml` with Caddy and Redis, `Caddyfile`, `.env.example`
- [ ] `README.md` with install paths and all connection snippets; `docs/`
- [ ] `scripts/install.sh`
- Acceptance: fresh VPS → `docker compose up` → `https://brain.<domain>/admin` → create project and key in the browser → Hermes on another machine connects and syncs.

### Phase 3 — Integrations and scale (v0.3)
- [ ] Redis pub/sub adapter and Redis locks; multi-worker verified
- [ ] GitHub webhook ingestion, pull-with-conflict policy, path → section mapping
- [ ] `sync_db_schema` tool and `brain db-watch` (PostgreSQL, then MySQL)
- [ ] PostgreSQL metadata verified in CI; Alembic migrations introduced
- [ ] `/api/v1` REST mirror of read tools for non-MCP agents
- [ ] Rate limiting, size caps, secret-pattern guard
- [ ] Approval-gated writes: `rw-approve` permission, `proposals` table, Approvals panel, `get_proposal` tool
- [ ] Watch lists (`## Sync Policy`), `importance` on mutations, relevance-filtered deltas and banner
- Acceptance: human push appears in every agent's log delta within seconds; schema change made by a migration shows up in `DB_SCHEMA_STATE.md` without agent involvement.

### Phase 4 — Hardening and release (v1.0)
- [ ] `brain doctor`, structured logging, Prometheus `/metrics`
- [ ] Backup and restore documentation and `brain vault export|import`
- [ ] Publish to PyPI and GHCR; versioned docs site
- [ ] One-click deploy templates: Railway, Render (`render.yaml`), Fly.io (`fly.toml`), DigitalOcean App Platform; Cloudflare Tunnel guide; evaluate a Cloudflare Containers target (§23)
- [ ] Security review pass against §25; `pip-audit` in CI
- [ ] Example repo: three agents (Hermes, Claude Code, Python cloud agent) syncing on one project

### Later (not scheduled)
- Full-text and semantic `search_brain` tool
- Optional server-side LLM features, off by default, enabled only when `LLM_PROVIDER`, `LLM_API_KEY`
  and `LLM_MODEL` are set: summarize human GitHub commits into `TECH_AND_INFRA.md`, fill in
  `compact_summary` when an agent omits it, an "Ask the brain" Q&A panel in the admin UI, and a
  semantic `search_brain`. The core server stays usable with none of these.
- Git history browser and section rollback in the UI
- Multiple admin users and SSO
- Slack/Discord notifications of hot-log entries
- Importers (Notion, Confluence, Google Docs) into the six files
- Per-project custom file sets beyond the canonical six
- Agent-private notes: a per-agent section or file readable and writable only by that agent and admin,
  so working state stays out of shared files.
- Per-agent sync policy overrides (cadence, watch list) in addition to the per-role `## Sync Policy`.

---

## 28. Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Python 3.11+, FastAPI, Starlette, FastMCP | Agreed in design conversation; matches Hermes ecosystem. |
| 2 | Six fixed canonical files per project | Predictable for agents, easy RBAC, small index. |
| 3 | Multi-tenant via `project_id`; one vault and one Git repo per project | Agencies host many clients on one VPS; isolation by directory and key. |
| 4 | Compound key `sb_<project_id>_<role>_<agent_id>_<secret>` | Human-readable, self-describing, routes without a lookup table while still being verified against the DB. |
| 5 | SHA-256 for key hashes (not bcrypt) | Secret is 32 random bytes; bcrypt would force scanning all keys. |
| 6 | SQLite default, PostgreSQL via `DATABASE_URL` | Zero-config local install; production option without code changes. |
| 7 | Redis optional | Local installs stay dependency-free; VPS can scale out. |
| 8 | Heading-level 8-char SHA-256 ETags | 95 %+ token savings; `NOT_MODIFIED` is nearly free. |
| 9 | Streamable HTTP primary, legacy SSE kept | Current spec plus older clients (some Hermes/OpenClaw builds). |
| 10 | Poll `get_company_log_delta` each turn in addition to notifications | Works with every MCP client, not only those that handle notifications. |
| 11 | `expected_etag` optimistic concurrency + per-heading locks | Sub-agents and parallel Hermes instances cannot lose updates. |
| 12 | `COMPANY_LOG.md` system-written, 50-entry hot log, monthly archives | Keeps the file small for agents; full history stays in Git and archives. |
| 13 | Single-file Tailwind admin UI served by the backend | No frontend toolchain for self-hosters. |
| 14 | Caddy for TLS and unbuffered SSE | One config block; automatic certificates. |
| 15 | Git auto-commit per mutation; push optional | Audit trail and backup for free; works offline. |
| 16 | Package at `src/second_brain/` with `brain` CLI | pipx/uvx install like Hermes and OpenClaw. |
| 17 | No LLM inside the server; agents bring their own model | Zero model cost for self-hosters, no provider lock-in, nothing to configure beyond URL and key. |
| 18 | Operating rules travel in-band over MCP (`instructions`, tool descriptions, `whoami`, sync banner) | Operators configure only URL and key; compliant clients need no prompt pasting. |
| 19 | One codebase (Python + Docker). One-click buttons target Docker-native hosts; Cloudflare is used for DNS, TLS and Tunnel, with Containers evaluated later | The Deploy to Cloudflare button cannot provision Containers and Workers cannot run our stack; a TypeScript port would be a rewrite. |
| 20 | Governance and noise-control mechanisms adapted from supermemory's company-brain architecture: approval-gated writes, relevance-filtered deltas, inline inbox, durable outbox, role-filtered tools | Proven in a multi-org commercial deployment; every one is implementable deterministically without an LLM. |

---

## 29. Open questions

1. Final product and repo name. Recommendation: product **AI Computer Brain**, repo `ai-computer-brain`,
   CLI `brain`. Reason: `company-brain` is already a 1k-star supermemory Slack bot (a different product,
   see §24), so any name containing "company brain" will be confused with it.
2. License: MIT (proposed) or Apache-2.0?
3. Should the Tailwind CSS be vendored for air-gapped installs, or is the CDN acceptable for v1?
4. Default role list: keep `admin, engineering, sales, marketing, ops, design` or add `finance`, `support`?
5. Exact Hermes and OpenClaw/mcporter config schemas must be verified against current releases before the README ships.
6. Should `/api/v1` REST read endpoints ship in Phase 1 instead of Phase 3 for non-MCP cloud agents?
7. One Uvicorn worker by default on VPS (simplest, no Redis locks) — confirm.
8. Should the template protect `COMPANY_PULSE.md#pricing-tiers` with `rw-approve` out of the box, or ship approvals fully opt-in (current plan)?

---

## 30. Not in scope for v1

- Running or hosting agents (that is Hermes, OpenClaw, Claude Code).
- Vector search or embeddings.
- A chat interface.
- Replacing GitHub, Notion or a CRM; the brain holds the *current truth summary*, not every document.
- Multi-user admin accounts (single master key in v1).
- Any built-in LLM. The server is model-free in v1; agents supply summaries and do merges. Optional
  server-side LLM features are listed under "Later" in §27.

---

## 31. Master prompt for Claude (generate the repository)

```
You are an expert full-stack AI systems engineer. Generate the complete, production-ready,
open-source repository "company-second-brain-mcp" exactly as specified in plan.md (this file is in
the repo root; read it first and treat it as the specification).

Summary of what to build: a self-hosted, multi-tenant Model Context Protocol server in Python 3.11+
using FastAPI, Starlette, FastMCP (official mcp SDK), SQLAlchemy 2 async (SQLite by default,
PostgreSQL via DATABASE_URL), optional Redis pub/sub, Git-backed per-project vaults, a single-file
Tailwind CSS admin dashboard at /admin, and a Typer CLI named `brain`. Each project owns six
canonical Markdown files (DESIGN_SYSTEM.md, TECH_AND_INFRA.md, DB_SCHEMA_STATE.md, COMPANY_LOG.md,
ACCESS_PERMISSIONS.md, COMPANY_PULSE.md) and keeps heterogeneous agents (multiple Hermes instances
and their sub-agents, OpenClaw, Claude Code, Serena hosts, custom cloud agents) in sync over
Streamable HTTP (/mcp), legacy SSE (/sse) and a plain SSE event stream (/events).

Implement, in this order, with complete logic and type hints, no stubs:

1. pyproject.toml, requirements.txt, .env.example, src/second_brain/config.py, database.py, models.py,
   sql/01_init_metadata.sql (projects, api_keys, mutation_logs, agent_sessions; dialect-aware).
2. templates/default_vault/*.md with the headings listed in plan.md §10 and an "Agent Notes" section.
3. markdown_engine.py: fenced-code-aware heading parser for ## and ###, GitHub slugs, 8-char
   SHA-256 ETags per section and per file, surgical replace/append/create/delete that leaves the rest
   of the file byte-identical, atomic writes.
4. auth.py: compound keys sb_<project_id>_<role>_<agent_id>_<secret> with [a-z0-9-] identifiers and
   a 32-byte base62 secret, SHA-256 hashing, constant-time verification, Bearer / X-Brain-Key / ?key
   extraction, Starlette middleware that sets a principal contextvar, admin master key handling,
   revocation.
5. rbac.py: parse the File Matrix and Heading Overrides tables of ACCESS_PERMISSIONS.md, cache by
   ETag, fail closed, admin always rw except COMPANY_LOG.md.
6. locks.py and hub.py: per-(project,file,heading) asyncio locks; in-process broadcaster with an
   optional Redis adapter selected by REDIS_URL.
7. company_log.py: 50-entry hot log in COMPANY_LOG.md, monthly archives in logs/, ledger queries.
8. git_sync.py: per-project background commit worker (2 s debounce, agent as author), optional push
   with retry; webhooks.py: /webhooks/{project_id}/github with HMAC verification, hot-log entries
   per commit, pull --rebase with server-wins conflict policy and ETag rebroadcast.
9. server.py: FastMCP tools whoami, get_brain_index, read_brain_section, read_brain_file,
   update_brain_section (modes replace/append/create, expected_etag → CONFLICT, subagent_role,
   RBAC → FORBIDDEN), get_company_log_delta, sync_db_schema, list_projects (admin only); resources
   brain://{project_id}/{FILE}.md[#slug] and brain://{project_id}/index; prompt brain_bootstrap;
   resource update notifications; mount /mcp and /sse; /events SSE with 20 s heartbeats.
10. web_ui.py + templates/admin_dashboard.html: login, projects, keys (create with one-time reveal and
    ready-made Hermes/OpenClaw/Claude Code snippets, revoke), file viewer/editor with If-Match,
    live activity, live agents, settings (git remote, webhook secret, export). JSON API under
    /api/admin/* protected by the master key; /healthz public.
11. cli.py: init, serve, project create|list|delete, key create|list|revoke, vault export|import,
    log tail, db-watch (db_watch.py: PostgreSQL information_schema introspection → sync_db_schema),
    doctor.
12. Dockerfile (non-root, git installed), docker-compose.yml (brain-server, redis, caddy),
    docker-compose.local.yml, Caddyfile with flush_interval -1 and 24h timeouts, scripts/install.sh.
13. tests/ covering plan.md §26, runnable with pytest.
14. README.md: problem statement, install (pipx, docker local, docker VPS with Cloudflare options),
    create project and keys (UI and CLI), the agent sync protocol, connection snippets for Hermes
    (~/.hermes/config.yaml), OpenClaw (mcporter), Claude Code, mcp.json clients, Python cloud agents,
    non-MCP SSE consumers, PostgreSQL switch, backups, security notes.

The server must not import or call any LLM provider SDK. It is a model-free data and sync layer; the
calling agents write compact summaries and perform merges. No LLM credentials appear in config.

Deliver the operating rules in-band as specified in plan.md §15.1: set the FastMCP server
`instructions` to the compact sync protocol, write every tool description as an operating rule (when
to call, what to pass), return the role-filtered rules and file guide from whoami, track
api_keys.last_sync_log_id on every get_company_log_delta call, and prefix every tool result with a
one-line sync banner when the caller has unread ledger entries.

Also implement, per plan.md §12, §14, §15.1, §18, §21, §22, §23 and §25: role-filtered tools/list
with list_changed notifications; the inline inbox (up to three unread relevant entries appended to
every tool result); `## Sync Policy` watch lists and an `importance` field on mutations with
relevance-filtered deltas; the `rw-approve` permission with a proposals table, PENDING_APPROVAL
responses, a get_proposal tool and an Approvals panel; a durable `pending_jobs` outbox for commits,
pushes and broadcasts replayed on startup; an inline diff per activity entry; a first-run setup
wizard and a Rules panel in the admin UI; `brain serve --tunnel` using a cloudflared quick tunnel;
safe defaults (push, webhooks, approvals and tunnel off until configured); and deploy templates for
Railway, Render, Fly.io and DigitalOcean.

The admin dashboard must work both served from the same process at /admin and as a static file
hosted on another origin: read an API base URL from a meta tag or a login-screen field, send the
master key only as a bearer token typed by the admin, honour CORS_ORIGINS on /api/admin/* and
/events, and document the Cloudflare DNS, Tunnel and Access steps in docs/cloudflare-domain.md.

Constraints: project isolation always derives from the key, never from tool arguments (except the
admin master key). File names are validated against the six-name allow-list. Every write produces a
ledger row, a hot-log line, a Git commit and a room broadcast. Compact summaries are capped at 160
characters. Write clean, modular, production-grade code with complete error handling. Do not omit
files or stub out logic. Generate the complete repository now.
```

---

## Addendum (2026-10-15): agent changelog, daily digest, admin hardening

Added after the first implementation pass, in response to two requirements raised during the build.

### Seventh canonical file: `AGENT_CHANGELOG.md`

- One `##` section per department plus a `## Daily Digest` section. Each agent writes only its own
  department section, and only through the `log_agent_activity` tool; direct edits are admin-only
  (`r` for every other role in the file matrix).
- `log_agent_activity(summary, kind=task|finding|decision|blocker, affects=[departments], details?,
  importance?, subagent_role?)` stamps the UTC date and time, prepends the entry (newest first, 100
  kept per department), records a ledger row with `event_type=activity_<kind>`, broadcasts, and
  rebuilds the Daily Digest.
- `affects`: the departments that must act on the entry. The ledger gained an `affects` column.
  Section writes fill it automatically with every department whose Sync Policy watches the changed
  heading (writer excluded). `get_company_log_delta` returns such entries to those departments with
  `relevance: affects_you` and an action hint, independent of their watch list.
- Daily Digest: rebuilt deterministically from the ledger after each activity, one `###` block per
  day for the last 7 days with a one-line summary and every task, finding, decision, blocker, brain
  update, schema sync and human commit. `get_daily_digest(date?)` returns the same data as JSON.
- Agent Rules in the template now require logging after every task, treating `affects_you` entries
  as work, and logging the day's findings before a session ends. `whoami` reports what the agent
  logged today so the server can tell whether findings are missing.

### Admin access hardening

- `ADMIN_PATH` (default `/admin`) serves the dashboard at a configurable, unguessable path; `/`
  redirects there.
- `ADMIN_ALLOWED_IPS` (comma-separated IPs or CIDRs) restricts the dashboard and `/api/admin/*` by
  client address; forwarded addresses from Caddy are honoured.
- `ADMIN_LOGIN_FAILURES_PER_MINUTE` (default 10) locks an address out of the admin API after
  repeated wrong keys. Agent endpoints are unaffected.

### Also changed during the build

- MCP SDK 2.x (`MCPServer`) is the target; routes are registered on the FastAPI router rather than
  mounted so `/mcp` works without a trailing slash. DNS-rebinding protection is disabled because the
  server sits behind a reverse proxy and authenticates every call with a key.
- Streaming responses no longer perform database writes during cancellation; cleanup runs in a
  background task and database transactions finish under `asyncio.shield`.

### Refinements to the changelog (same day)

- The changelog holds brain-level items only: changes other departments rely on, things shipped,
  new findings (a feature, a service, a customer need, a problem), decisions, blockers, new
  capabilities. Routine steps stay in each agent's own memory on the server where it runs.
- Compaction is deterministic and server-side, so it is unbiased: an entry equal (after
  normalisation) to one logged in the last `CHANGELOG_DEDUPE_HOURS` by any department returns
  `DUPLICATE` and is not written; each department keeps `CHANGELOG_ENTRIES_PER_DEPARTMENT` entries
  and older ones move to `logs/changelog-YYYY-MM.md`; the Daily Digest keeps seven days.
- Agents joining (key issued) and leaving (key revoked) are written to the department section by
  the server with `affects: all`, so every agent learns about a new colleague on its next delta.
- `COMPANY_LOG.md` gained a `## Findings` section: the latest `FINDINGS_IN_COMPANY_LOG` findings
  from any department with the departments they affect, rebuilt whenever a finding is logged.
- The admin panel shows sync health per project: agents in sync versus stale, last GitHub event,
  last changelog entry. GitHub is connected per project in Settings (remote plus webhook).
- Atomic file replacement retries briefly on Windows, where a concurrent reader can block
  `os.replace` for a few milliseconds.

### One-click API keys (same day)

- The admin panel no longer asks for project IDs, roles or agent IDs up front. The first-run screen
  takes a company name and a key name; the API keys tab takes a key name. `POST /api/admin/keys/quick`
  (and `brain key quick "<name>"`) creates the project when needed, derives the agent ID from the
  name, picks the role from department words in the name (admin when none), issues the key, and
  returns the MCP endpoint plus ready-to-paste commands for Claude Code, Hermes, OpenClaw, mcp.json,
  Python and curl. Role and agent ID remain editable under Advanced; duplicates get a -02 suffix.

### One-command start (2026-10-16)

- `start.sh` (macOS, Linux) and `start.cmd` / `start.ps1` (Windows, double-clickable) create the
  virtual environment with `uv` or any Python 3.11+, install the package on first run, and launch
  `brain serve --open`.
- `brain serve` now bootstraps itself: with no master key configured it writes `.env`, generates the
  key and prints it. `--open` waits for the health check and opens the admin panel with a one-time
  `?key=` parameter that the dashboard consumes and strips from the URL, so the first screen is the
  signed-in "Create API key" form. `brain init` remains for scripted setups.

### Interactive CLI (2026-10-16)

- Bare `brain` starts the server. On the first run in a terminal it asks where the server will
  live (this computer, or a VPS with a domain), the port, and whether to create the first project
  and API key; it writes `.env`, prints the key and the agent command, and for a VPS prints the DNS,
  firewall and `docker compose up -d` steps. Non-interactive runs (Docker, systemd) bootstrap
  silently; `--no-wizard` forces that.
- While running, the CLI shows a status box: admin panel URL, MCP endpoint, public URL, master key
  location, projects, and the next step. Request logs are hidden unless `--verbose` is passed.

### Terminal console (2026-10-16)

- `brain` in a terminal now opens a console (`src/second_brain/tui.py`): the server runs in a
  background thread and the terminal shows a menu. 1 creates a project and its first key from a
  name alone; 2 manages every project's keys, Git remote and webhook; 3 shows and follows activity;
  4 connects a Cloudflare quick tunnel, a VPS domain, Railway or Render, or protects the admin
  panel; 5 edits `.env` with masked secrets and restarts; 6 shows or rotates the master key and
  sets the admin path, IP allowlist, lockout and local-only binding. The console talks to the
  running server through the admin API, so it sees exactly what the browser panel sees.
- `start.sh` and `start.cmd` launch the console with the browser panel opened and signed in.
  `brain serve` stays the plain server for Docker and systemd.

### Colour themes (2026-10-16)

- The console asks for a colour theme on first use and stores it as `BRAIN_THEME` in `.env`.
  Seven palettes (Ocean, Ember, Forest, Violet, Sunset, Mint, Mono) are shown as live swatches;
  `t` changes the theme later. Panels, tables, menu keys, success, warning and error messages all
  use the palette through a rich `Theme`, and the plain `brain serve` banner follows it too.
- Console output is kept to ASCII apart from rich's own box drawing, so legacy Windows code pages
  render it without errors. `BRAIN_FORCE_INTERACTIVE=1` and `BRAIN_NONINTERACTIVE=1` override the
  terminal detection for scripted runs.

### Hermes-style shell, pixel logo, documents, GitHub analysis (2026-10-16)

- The console (`src/second_brain/tui.py`) is now a shell. It opens with `AI COMPUTER BRAIN` drawn
  as 7-row pixel letters (`src/second_brain/banner.py`, ASCII-safe, background-coloured cells in
  three bands derived from the theme's primary and accent colours, single, two or three lines
  depending on the terminal width) with `powered by Cognio Labs` beside the version line. The
  prompt is `brain @project>`; slash commands cover projects, keys, files, uploads, documents,
  activity, agents, GitHub, LLM, connectivity, security and settings; anything without a slash runs
  in the user's shell, with `ls`, `cat`, `cd`, `pwd`, `grep`, `head`, `tail` and `clear` built in
  so they work on Windows too. Unknown commands get a closest-match hint.
- Menus use arrow keys (`src/second_brain/picker.py`: Up/Down, PageUp/PageDown, Home/End, type to
  filter, Enter, Esc) rendered through rich `Live`; terminals without key support fall back to a
  numbered prompt, which is also what scripted runs and tests use.
- Projects are created from the name alone everywhere: `POST /api/admin/projects/quick` and
  `/new` derive a unique project ID, issue the first key (`primary-agent`, admin) and return the
  key, the MCP endpoint and the connection snippets. The panel's sidebar form asks only for the name.
- Documents (knowledge base): `documents` table, `docs/` folder inside each vault, text extraction
  for Markdown, text, CSV, JSON, YAML, HTML, PDF (pypdf) and Word (.docx). Each document carries an
  access list (`*`, agent IDs, `role:<role>`). Admin API under `/api/admin/projects/{id}/documents`;
  MCP tools `list_documents`, `search_documents`, `read_document` (chunked by offset) visible to
  every role; `whoami` lists the documents shared with the agent; uploads, sharing changes and
  removals are logged with `affects` set to the roles that may read the document.
- Brain file upload: `POST /api/admin/projects/{id}/files/{name}/upload` replaces a canonical file
  from an uploaded Markdown file (`mode=replace`) or merges it into the existing headings through
  the LLM (`mode=smart`).
- Optional LLM (`src/second_brain/llm.py`): providers OpenAI, Anthropic (official SDK), Google
  Gemini, OpenRouter, Amazon Bedrock (OpenAI-compatible endpoint per region) and custom
  OpenAI-compatible endpoints. Settings `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`
  persisted to `.env` through `PUT /api/admin/llm`; `POST /api/admin/llm/models` verifies a key and
  lists the chat models it can use; `POST /api/admin/llm/test` sends a one-word check.
- Repository analysis (`src/second_brain/repo_analysis.py`): per-project GitHub settings (URL,
  branch, token, analyse-on-push) under `settings_json.github` with the token never returned by the
  API; shallow clone into `brain_files/.repos/<project>` with the token sent as an HTTP header and
  credential prompts disabled; digest of manifests, infra files, migrations and ORM models, design
  tokens, routes, docs and the last 30 commits with secrets redacted; one JSON-only model call that
  returns sections for `DESIGN_SYSTEM.md`, `TECH_AND_INFRA.md` and `COMPANY_PULSE.md`, a
  `db_schema` payload fed to `sync_db_schema`, and findings logged through `log_agent_activity` as
  agent `repo-analyst`; unchanged sections are skipped; progress steps are published as
  `repo_analysis` events and the last result is stored on the project. A GitHub push webhook
  triggers a run when the LLM is connected and analyse-on-push is on, with overlapping runs
  coalesced.
- Admin panel rebuilt as a token-based dark/light product UI (no Tailwind): theme-coloured pixel
  mark, sidebar with projects and pages (Overview, Agents, Brain files, Documents, Activity,
  Approvals, Rules, GitHub & LLM, Settings), one-field project creation with the connection card,
  inline revoke/delete confirmations, drag-and-drop document upload with per-agent sharing, file
  upload with replace or smart merge, LLM setup in two steps (credentials, then a model list loaded
  from the provider), and live analysis progress.
- New settings in `.env.example`; new dependencies `anthropic` and `pypdf`. Tests: 70 (console and
  logo, LLM config and model parsing, analyser with a real repository and a stubbed model,
  documents store and MCP tools, admin endpoints).

### Sign-in without an API key, and the Assistant (2026-10-16)

- `LLM_AUTH=login` makes the Anthropic provider use the official Anthropic CLI profile (`ant auth
  login`) instead of a key: the SDK resolves the credentials itself, the brain stores no secret.
  The console runs `ant auth login` (with `--no-browser` on request) and then lists the account's
  models; the panel drives `ant auth login --no-browser` through `POST /api/admin/llm/login/start`
  (returns the sign-in link) and `POST /api/admin/llm/login/complete` (takes the pasted code,
  verifies by listing models, saves provider, auth mode and model). `GET /api/admin/llm/login`
  reports whether the CLI is installed, with install instructions per platform. OpenAI and Gemini
  have no supported third-party login, so they keep API keys; subscription logins that reuse
  another product's OAuth client were deliberately not implemented.
- Assistant (`src/second_brain/chat.py`): `POST /api/admin/projects/{id}/chat` takes the
  conversation, builds the context from the seven brain files, the last 25 activity entries, the
  document list and keyword-matched passages from the uploaded documents, and asks the model for
  a JSON answer with a Markdown reply and optional changes (file, heading, mode, content, summary)
  limited to the five admin-writable files. `POST .../chat/apply` applies approved changes through
  `update_section` as agent `assistant` (creating the heading when it does not exist), so they are
  logged, committed and broadcast like any agent write. The panel has an Assistant page with the
  conversation and proposed-change cards (apply all, apply one, dismiss); the console has `/chat`.

### Account sign-in, backup repositories, disk watcher, two-step delete (2026-10-16)

- `LLM_AUTH=oauth` (`src/second_brain/oauth.py`): PKCE sign-in for Claude accounts (Claude Code's
  public client, code pasted from console.anthropic.com), ChatGPT accounts (the Codex CLI client with
  a local callback on port 1455 or a pasted address, then a token exchange for an API key with the
  ChatGPT Responses backend as fallback) and Google accounts (the Gemini CLI client, Code Assist
  onboarding and `generateContent`). Tokens live in `data/oauth.json` (mode 600) and refresh
  themselves. Endpoints `GET /api/admin/llm/oauth`, `POST .../oauth/start`, `POST .../oauth/wait`,
  `POST .../oauth/complete`, `DELETE .../oauth/{provider}`. The console `/llm` offers the sign-in
  first for those providers; the panel shows the sign-in step before the model list. Decision: the
  user asked for these flows explicitly, knowing they reuse other products' OAuth clients; API keys
  remain the documented fallback.
- Backup repositories (`src/second_brain/github_api.py`): `GITHUB_TOKEN`/`GITHUB_OWNER` connect a
  GitHub account (`/api/admin/github-account`); `POST /api/admin/projects/{id}/backup` or the
  `backup` flag of `POST /api/admin/projects/quick` creates a private `<project>_brain` repository,
  sets it as the vault's Git remote (token sent as `x-access-token` in the push URL on this server
  only, masked everywhere else) and pushes immediately; the Git worker pushes every later commit.
  `quick_project` also accepts `repo_url` to connect the company repository in the same step.
- Disk watcher: `sync_disk_changes` runs every `DISK_WATCH_SECONDS` (60): uncommitted changes in a
  vault are committed; hand-edited canonical files are logged as `human_pull` entries with
  `disk edit` summaries and broadcast per section; server-written leftovers are committed quietly;
  a push is queued when a remote exists.
- Delete: `DELETE /api/admin/projects/{id}?delete_backup=true` returns the outcome; the panel has a
  Delete button in the project header with a two-step confirmation and the option to remove the
  backup repository; the console has `/delete`.
- Brain files page rebuilt: file list, toolbar (rendered or source view, upload, download, edit
  file), section chips that open the section editor, proper table and heading styles.
- Tests: 81.

### Own domain from this computer (2026-10-16)

- `/domain` is a guided flow: it explains how a domain can reach a locally running brain, asks
  where the brain will be reached (this computer through a Cloudflare Tunnel, a VPS with Caddy, or a
  hosting platform), validates the domain or subdomain, and then either drives the tunnel end to
  end or prints the steps and offers a reachability check.
- `src/second_brain/cloudflare.py`: `cloudflared tunnel login` (browser, once), `tunnel create`
  (or reuse by name via `tunnel list --output json`), `tunnel route dns --overwrite-dns`, a config
  file under `data/cloudflared-<name>.yml` with an ingress rule to the local port, `tunnel run`
  logging to `data/cloudflared-<name>.log`, and a health wait on `https://<domain>/healthz`.
  `CLOUDFLARE_TUNNEL=<name>` in `.env` makes the console and `brain serve` start it again.
- The console status box shows the domain state (none, temporary tunnel URL, or the domain with
  its tunnel live or stopped) and what the server is reachable from.

### Effort, empty templates, document metadata, agent guidance (2026-10-16)

- `LLM_EFFORT` (default, low, medium, high; Anthropic also xhigh and max) is asked after the model
  in `/llm` and shown as a select in the panel; applied as `output_config.effort` on Anthropic,
  `reasoning_effort` on OpenAI-compatible endpoints (dropped automatically when a model rejects it)
  and `reasoning.effort` on the ChatGPT backend.
- The four content templates (`DESIGN_SYSTEM.md`, `TECH_AND_INFRA.md`, `DB_SCHEMA_STATE.md`,
  `COMPANY_PULSE.md`) ship with placeholder lines only (`- (no ... yet)`, matching the existing
  placeholder prefix) and an Agent Notes rule that such a section holds no facts; the generic
  protocol repeats the rule.
- Documents: when an LLM is connected, `DocumentStore.describe` runs in the background after an
  upload and stores `{summary, topics, departments, model, at}` as JSON in the `summary` column;
  `brief()`/`to_dict()` expose them, `search_documents` also matches title, summary and topics,
  the panel shows them under the title.
- `whoami` carries `sources` (backup repository URL, company repository URL, `docs/`), and
  `connect_github` starts the repository analysis immediately when an LLM is enabled.
- The key card (console and panel) ends with "What the agent does once connected".
- Tests: 89.

### Background service, Hermes-style (2026-10-16)

Hermes deploys with `hermes gateway install`: a systemd user or system service, a launchd agent or a Windows scheduled task that starts the gateway at boot and keeps it alive. The brain now does the same.

- `src/second_brain/background.py`: `Spec`, `Status`, templates (`render_systemd`, `render_launchd`, `render_vbs` + `render_task_xml`), `install/start/stop/restart/uninstall/status/logs/describe`, PID file written by `brain serve` (`data/brain.pid`), `pid_is_brain` guard before killing, drift detection against the saved definition.
- CLI: `brain service install [--system] | start | stop | restart | status | logs | uninstall`; `brain doctor` reports the service; `brain serve` refuses to start when the service already answers on the port.
- Console: `/service` (picker or sub-command); the console attaches to a running service instead of starting its own server, `/restart`, `/set`, `/protect`, `/rotate` and `/domain` restart the service, the status box shows a Background row, and `/domain` offers the service once the domain is live so the tunnel survives closing the console and rebooting.
- Wizard: asks "Run the brain in the background, starting when you log in?" (default yes) and installs it; new option 3 "This computer, on your own domain through Cloudflare" pre-downloads cloudflared.
- cloudflared is downloaded automatically whenever it is needed (`/domain` in the background while the user answers, `/tunnel`, `brain serve --tunnel`, resuming a saved tunnel).
- `scripts/install.ps1`: Windows one-liner (`iex (irm .../install.ps1)`) that installs git if needed, clones and runs `start.ps1`.
- Docs: README "Keep it running in the background", docs/deployment-vps.md "Without Docker", docs/cloudflare-domain.md "Keeping the tunnel up" and "What the tunnel ID is".

### Keys viewable again, agent connect card (2026-10-16)

The one-time key reveal confused the user ("what MCP connection do I give my agents?"). Decision 5 is amended: keys are still verified by SHA-256 hash, but `auth.py` now also stores a copy sealed with Fernet under a key derived from `ADMIN_MASTER_KEY` (`keyvault.py`, column `api_keys.secret_sealed`, added to old databases by `COLUMN_UPGRADES` in `database.py`). `GET /api/admin/projects/{id}/keys/{key_id}/connect` returns the key and paste-ready snippets, `POST .../reissue` replaces a key, `POST /api/admin/keys/reseal` re-seals all keys when the master key rotates (`/rotate` calls it first). The Overview opens with a "Connect an agent" card (agent picker, endpoint, key with show/copy, snippet tabs); the Agents page has connect and new key per row; the console has `/agent`. Keys created before this change have no sealed copy and show a placeholder plus "Issue a new key".

### Vault history moved out of the vault folder (2026-10-16)

VS Code listed every vault as a Git repository with "Publish Branch" buttons, which the user read as GitHub asking for something. `GitRepo` now takes a `git_dir` and runs git with `--git-dir`/`--work-tree`; the service keeps each history in `brain_files/.git-store/<project_id>` (`BrainService.git_repo`), so `brain_files/<project>/` holds only the Markdown files. `adopt_vault_histories()` runs at startup and moves any legacy `brain_files/<project>/.git` into the store, history intact. `.vscode/settings.json` additionally stops VS Code from scanning `brain_files`, `data`, `.venv` and `.pytest_tmp` for repositories.

### Home folder, no clone needed (2026-10-16)

`brain` from a pipx or uv tool install must work without a checkout, like `hermes` with `~/.hermes`. `config.brain_home()` picks `$BRAIN_HOME`, else the current folder when it holds a `.env` (clone-and-run and existing installs keep working), else `~/.brain`; `enter_home()` creates it and changes into it before any command runs (`cli.main`). The wheel already carries `templates/` and `sql/` through hatch `force-include`, and `Settings` resolves them inside the package first. The wizard and the status box print the folder; `install.sh` no longer creates its own directory. Tests set `BRAIN_HOME` to a temporary folder through an autouse fixture.
