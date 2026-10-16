# Company Log

The hot activity stream: the most recent changes made by agents, humans and automated watchers across this project. Written by the server only.

## Hot Log

- (no entries yet)

## Findings

- (no findings yet)

## How To Read This File

Hot Log: one line per change, `#id time actor → target — summary (old→new etag)`, newest first. The server keeps the last 50 entries here and moves older ones to `logs/archive-YYYY-MM.md`. Query the same data with `get_company_log_delta` instead of reading this file.
Findings: the latest discoveries any department logged with `log_agent_activity(kind=finding)`, such as a feature, service, customer need or problem found by a sales, marketing, production or engineering agent, with the departments it affects.

## Agent Notes

Do not write to this file. Every `update_brain_section`, approved proposal, GitHub push, schema sync and logged activity is recorded here automatically; findings are collected from the agent changelog.
