# Architecture

AI Computer Brain is one Python process that serves three audiences from the same data: agents over MCP, administrators over a JSON API and a static dashboard, and automated systems over webhooks.

```
agents ──MCP /mcp, /sse──┐
agents ──SSE  /events────┤        ┌──────────── service layer ────────────┐
admin  ──JSON /api/admin─┼──auth──┤ BrainService: RBAC, locks, writes,    ├── vaults (Git repos on disk)
browser ─GET  /admin─────┤        │ ledger, proposals, deltas, banner      ├── metadata DB (SQLite/Postgres)
GitHub ──POST /webhooks──┘        └───────┬───────────────┬───────────────┘
                                          │               │
                                   pending_jobs       event hub
                                   (durable outbox)   (in-process or Redis)
                                          │               │
                                     Git worker      MCP notifications, /events streams, admin stream
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Settings from environment variables, canonical file names, identifier rules |
| `database.py` | Async SQLAlchemy engine, applies `sql/01_init_metadata.sql` with dialect translation |
| `models.py` | ORM models: projects, API keys, mutation ledger, agent sessions, proposals, pending jobs |
| `auth.py` | Compound key generation, parsing, SHA-256 hashing, verification, ASGI auth middleware, rate limiter |
| `rbac.py` | Parses `ACCESS_PERMISSIONS.md` into a policy: file matrix, heading overrides, sync policy, rules, file guide |
| `markdown_engine.py` | Fence-aware heading parser, slugs, 8-char etags, surgical replace, append, create, delete |
| `locks.py` | Per-heading asyncio locks, optional Redis locks for multi-worker installs |
| `hub.py` | Per-project event rooms, in-process by default, Redis pub/sub when configured |
| `vault.py` | Project directories, atomic writes, template provisioning, export and import |
| `company_log.py` | Ledger queries, relevance-filtered deltas, the 50-entry hot log and monthly archives |
| `git_sync.py` | Git operations, durable job worker for commits, pushes, broadcasts and webhook pulls |
| `service.py` | The operations every transport shares |
| `server.py` | MCP tools, resources, prompt, tool visibility middleware, ASGI composition |
| `web_ui.py` | Admin JSON API, `/admin` page, `/events` stream |
| `webhooks.py` | GitHub push ingestion |
| `db_watch.py` | PostgreSQL schema introspection client for `brain db-watch` |
| `cli.py` | The `brain` command |

## A write, end to end

1. The ASGI auth middleware hashes the bearer key, loads the key row, checks that the key's embedded project, role and agent match the row, and attaches a `Principal` to the request.
2. The MCP tool resolves the project from the principal. The master key must name a project; agent keys may not name another one.
3. `BrainService.update_section` validates the file name, mode, summary, size and secret patterns, then asks the policy for the role's permission on `file#slug`. `rw-approve` turns the write into a proposal.
4. The per-heading lock is taken. The file is parsed, the section located, and the caller's `expected_etag` compared with the current one. A mismatch returns `CONFLICT` with the current body.
5. The section is rewritten and the file written atomically. Nothing outside the section changes.
6. In one database transaction the ledger row is inserted along with a `broadcast` job and a debounced `git_commit` job. The hot log in `COMPANY_LOG.md` is rewritten; overflow lines go to `logs/archive-YYYY-MM.md`.
7. The Git worker publishes the broadcast to the hub, which fans out to `/events` streams, the admin activity stream and MCP resource-updated notifications. A few seconds later it commits the vault, records the commit hash on the ledger row, and pushes if a remote is configured.
8. The next time any other agent calls a tool, the response carries a sync banner with the unread count, or the entries themselves when there are three or fewer relevant ones.

## Why these choices

- **Markdown files, not a database, for knowledge.** Humans can read and edit them, Git versions them, and agents handle them natively.
- **Heading-level etags.** A section is the unit agents care about. Unchanged sections cost a handful of tokens to confirm.
- **Polling plus push.** Many MCP clients ignore server notifications. The per-turn delta call works everywhere; notifications and the banner make it faster.
- **Durable outbox.** A crash between the file write and the Git commit cannot lose the commit or the broadcast.
- **No model in the server.** Summaries come from the writing agent, merges from the reading agent. The server stays deterministic and free to run.
- **One codebase.** The same Docker image runs on a laptop, a VPS, Railway or Render.

## Scaling notes

A single worker handles text edits for many agents comfortably. For more than one worker set `REDIS_URL`; locks and the event hub then span processes. PostgreSQL replaces SQLite with one environment variable. Vaults are plain directories, so disk is the only state that must be persistent, and a Git remote makes even that recoverable.
