# Access Permissions

Who may read and write what, and the operating rules served to every agent in this project. Only admin keys and the admin panel may edit this file.

## Roles

admin, engineering, sales, marketing, ops, design

## File Matrix

| File | admin | engineering | sales | marketing | ops | design |
|---|---|---|---|---|---|---|
| DESIGN_SYSTEM.md | rw | r | r | rw | r | rw |
| TECH_AND_INFRA.md | rw | rw | r | r | rw | r |
| DB_SCHEMA_STATE.md | rw | rw | r | r | r | r |
| COMPANY_LOG.md | r | r | r | r | r | r |
| ACCESS_PERMISSIONS.md | rw | r | r | r | r | r |
| COMPANY_PULSE.md | rw | r | rw | rw | rw | r |
| AGENT_CHANGELOG.md | rw | r | r | r | r | r |

## Heading Overrides

| Target | Role | Permission |
|---|---|---|
| COMPANY_PULSE.md#pricing-tiers | marketing | r |
| TECH_AND_INFRA.md#recent-changes | sales | r |

## Sync Policy

| Role | Watch | Cadence |
|---|---|---|
| admin | * | every-turn |
| engineering | TECH_AND_INFRA.md, DB_SCHEMA_STATE.md, COMPANY_PULSE.md#current-priorities | every-turn |
| sales | COMPANY_PULSE.md, DESIGN_SYSTEM.md#tone-of-voice | every-turn |
| marketing | COMPANY_PULSE.md, DESIGN_SYSTEM.md | every-turn |
| ops | COMPANY_PULSE.md, TECH_AND_INFRA.md | every-turn |
| design | DESIGN_SYSTEM.md, COMPANY_PULSE.md#shipped-recently | every-turn |

## Agent Rules

- At session start call `whoami`, then `get_brain_index`, then read the sections your role depends on and keep their etags.
- At the start of every turn call `get_company_log_delta` with the last id you saw. Re-read only sections that changed.
- After you change something real (a price, a feature, a schema, a brand token, an endpoint, a decision) call `update_brain_section` with a one-line summary written for other agents and the etag from your last read.
- The brain is the summary, not the database: verify live details in the source system before acting on them, and never copy bulk data here.
- After work that other departments must know about (a change they rely on, something shipped, something new found, a decision, a blocker, a new capability), call `log_agent_activity` with a one-line summary, the kind (task, finding, decision, blocker) and `affects`: the departments that must adjust because of it. The server stamps the UTC date and time, writes it under your own department in AGENT_CHANGELOG.md, and drops exact duplicates. Keep routine steps in your own memory, not here. Never edit another department's section.
- When a delta entry is marked `affects_you`, treat it as work for you: read what changed, decide what in your own output must be adjusted, do it, and log that adjustment as a task.
- Before you finish a session, log your findings of the day with `kind=finding` so the Daily Digest is complete for everyone.
- Never write secrets, credentials or personal data into the brain.
- On CONFLICT re-read the section, merge your change, retry once.

## File Guide

| When you have just... | Update |
|---|---|
| Changed a price, plan, discount or packaging | COMPANY_PULSE.md#pricing-tiers |
| Shipped or launched a feature | COMPANY_PULSE.md#shipped-recently |
| Changed sprint focus or priorities | COMPANY_PULSE.md#current-priorities |
| Made or reversed a business decision | COMPANY_PULSE.md#open-decisions |
| Changed a colour, font, spacing or component rule | DESIGN_SYSTEM.md#brand-colors, #typography or #components |
| Changed tone-of-voice or copy rules | DESIGN_SYSTEM.md#tone-of-voice |
| Added, moved or removed a service, endpoint or environment | TECH_AND_INFRA.md#services-endpoints or #deployments |
| Upgraded a framework or runtime | TECH_AND_INFRA.md#stack |
| Ran a migration or altered a table | sync_db_schema |
| Changed who may edit what | ACCESS_PERMISSIONS.md (admin only) |
| Finished any task, with or without a brain change | log_agent_activity (kind task, affects the departments concerned) |
| Found something other departments should know | log_agent_activity (kind finding) |
| Hit a blocker or made a decision | log_agent_activity (kind blocker or decision) |
| Anything else other agents should know | COMPANY_PULSE.md#open-decisions |

## Notes

- Permission values: `r` read, `rw` read and write, `rw-approve` write becomes a proposal that an admin approves, `-` no access.
- Heading overrides win over the file matrix.
- `COMPANY_LOG.md` is written only by the server.
- `AGENT_CHANGELOG.md` is written through `log_agent_activity`, which only ever writes the caller's own department section and the Daily Digest. Direct edits are admin only.

## Agent Notes

Belongs here: roles, the file matrix, heading overrides, the sync policy, agent rules and the file guide.
Does not belong here: anything an agent would write during normal work. Ask an admin to change permissions.
