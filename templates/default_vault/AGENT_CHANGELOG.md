# Agent Changelog

The one changelog every agent in this project shares: brain-level changes other departments need to know about, with the date and time in UTC. Every agent writes only to its own department section, through the `log_agent_activity` tool. The server removes duplicates, archives old entries to `logs/changelog-YYYY-MM.md`, records agents joining or leaving, and compiles the Daily Digest from all departments, so no single agent decides what stays.

## admin

- (no entries yet)

## engineering

- (no entries yet)

## sales

- (no entries yet)

## marketing

- (no entries yet)

## ops

- (no entries yet)

## design

- (no entries yet)

## Daily Digest

- (no digest yet)

## Agent Notes

Belongs here: what other departments must know. A change you made that others rely on, something you built or shipped, something new you found (a feature, a service, a customer need, a problem), a decision, a blocker, a new capability or agent. Log it with `log_agent_activity(kind=task|finding|decision|blocker, summary, affects=[departments])` right after the work, and log your findings of the day before you finish a session.
Does not belong here: routine steps and internal notes, which stay in your own memory on the server you run on; edits to another department's section; long reports; anything that belongs in one of the other brain files.
Entries are newest first and carry the date, time and agent. An entry equal to one logged in the last 24 hours by any department is not written twice. `affects` tells other departments that a change concerns them: when design changes a style and marks `affects: marketing`, the marketing agent sees it in its next delta flagged as affecting it and adjusts its work.
