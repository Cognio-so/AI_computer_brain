# Connecting agents

Every agent needs two values: the MCP endpoint and its compound key.

| Value | Example |
|---|---|
| Endpoint | `https://brain.yourdomain.com/mcp` (local: `http://localhost:8000/mcp`) |
| Key | `sb_acme-corp_sales_hermes-sales-01_Qm3k9vXz2LpR8tYw4NbC7fHd1JgS6aEu0ZxVoKiM5nT` |

The key is sent as `Authorization: Bearer <key>`. Clients that cannot set headers may use `X-Brain-Key: <key>` or, as a last resort, `?key=<key>` in the URL.

Keys are issued in the admin panel (**Keys → Create agent key**) or with `brain key create --project acme-corp --role sales --agent hermes-sales-01 --snippets`. The panel shows the snippets below pre-filled with the real key.

## Hermes

Add the server to `~/.hermes/config.yaml` on each machine. Each Hermes instance gets its own key.

```yaml
mcp_servers:
  company-brain:
    url: https://brain.yourdomain.com/mcp
    headers:
      Authorization: "Bearer sb_acme-corp_sales_hermes-sales-01_..."
```

Hermes sub-agents share the parent's key. Ask them to pass `subagent_role` on writes so the ledger shows `sales/hermes-sales-01[ui-subagent]`. Confirm the exact config keys against the Hermes release you run.

## OpenClaw

```bash
mcporter config add company-brain \
  --url https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_ops_openclaw-home_..."
```

## Claude Code

```bash
claude mcp add --transport http company-brain https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_engineering_claude-code-01_..."
```

Claude Code injects the server's `instructions` into the session, so the sync protocol is active immediately. For automatic syncing at session start, add a `SessionStart` hook that calls the `whoami` and `get_company_log_delta` tools, or add one line to `CLAUDE.md`: "Before coding, call company-brain get_company_log_delta and read changed sections."

## Cursor, Windsurf, Claude Desktop and other `mcp.json` clients

```json
{
  "mcpServers": {
    "company-brain": {
      "url": "https://brain.yourdomain.com/mcp",
      "headers": { "Authorization": "Bearer sb_acme-corp_marketing_hermes-marketing-01_..." }
    }
  }
}
```

## Serena

Serena is an MCP tool server for coding, not an MCP client. Add the brain to the host that runs Serena, such as Claude Code or Cursor. Both servers are then available in the same session.

## Python, LangGraph, CrewAI, AutoGen, OpenAI Agents SDK

Use the official `mcp` package (2.x):

```python
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

KEY = "sb_acme-corp_engineering_analytics-01_..."
http = create_mcp_http_client(headers={"Authorization": f"Bearer {KEY}"})

async with streamable_http_client("https://brain.yourdomain.com/mcp", http_client=http) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        me = await session.call_tool("whoami", {})
        delta = await session.call_tool("get_company_log_delta", {"since_entry_id": me.structured_content["latest_log_id"]})
```

Frameworks with MCP adapters (LangGraph's `langchain-mcp-adapters`, CrewAI's MCP tools, the OpenAI Agents SDK `MCPServerStreamableHttp`) take the same URL and header.

## Slack, Discord and Telegram bots, n8n, Zapier

Any bot or workflow runner that can call MCP tool servers joins like any other agent, usually with an `ops` or `marketing` key. A bot that cannot speak MCP can still read through the REST-free path below.

## Without MCP: the event stream

```bash
curl -N -H "Authorization: Bearer sb_..." https://brain.yourdomain.com/events
```

A JSON Server-Sent Events stream of every change in the project, with a heartbeat every 20 seconds. Pair it with `read_brain_file` calls through any MCP client, or read the files from the Git remote.

## Legacy SSE transport

Older clients that only speak the HTTP+SSE transport connect to `https://brain.yourdomain.com/sse` with the same header.

## What the agent receives

On connect the server sends the sync protocol in the MCP `instructions` field. `tools/list` returns only the tools the role may use. `whoami` returns the role's permissions, watch list, rules, file guide, its own changelog section and what it has logged today. Every tool result carries a `sync` block when other agents changed something the agent has not seen, including the entries themselves when there are three or fewer.

## The changelog and the Daily Digest

Each department has its own section in `AGENT_CHANGELOG.md`. After every completed task the agent calls `log_agent_activity(summary, kind, affects)`; the server stamps the UTC date and time and writes the entry under the caller's department. An agent cannot write another department's section, through this tool or any other.

`affects` is how departments tell each other that something concerns them. When design logs a style change with `affects: ["marketing"]`, the marketing agent's next `get_company_log_delta` returns that entry with `relevance: affects_you` and a hint to adjust its own work and log the adjustment. Section writes get the same flag automatically for every department whose Sync Policy watches the changed heading, so a pricing change by sales reaches marketing and ops without anyone remembering to tag them.

Before finishing a session an agent logs its findings of the day with `kind=finding`. The server rebuilds the Daily Digest section after every entry: one block per day for the last seven days with a one-line summary and every task, finding, decision, blocker, brain update, schema sync and human commit, in order, with who did it and which departments it affects. `get_daily_digest` returns the same data for any date.

## Documents: the knowledge base

Documents the admin uploads (SOPs, specs, contracts, research) are shared with every agent of the project, with specific agents, or with every agent of a role. An agent sees the ones shared with it in the `documents` list of `whoami`, lists them with `list_documents`, finds passages with `search_documents` and reads them in chunks with `read_document(doc_id, offset, max_chars)`; the answer carries `next_offset` until the end. Large files never land in the context at once. New or re-shared documents appear in `get_company_log_delta` flagged `affects_you` for the departments that may read them.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401 missing API key` | The header did not reach the server. Check the client config and any proxy that strips `Authorization`. |
| `401 unknown or revoked API key` | The key was revoked or belongs to another server. Issue a new one. |
| `403 this key belongs to a different project` | The agent passed a `project_id` argument that does not match its key. Leave it empty. |
| `FORBIDDEN` on write | The role lacks `rw` on that file or heading. Check **Files → ACCESS_PERMISSIONS.md**. |
| `CONFLICT` | Another agent changed the section first. Read the returned `current_content`, merge, retry with `current_etag`. |
| `PENDING_APPROVAL` | The role has `rw-approve`. An admin decides in **Approvals**; poll `get_proposal`. |
| Stream drops every 100 seconds | A proxy without SSE support, or Cloudflare without the heartbeat reaching it. Keep `flush_interval -1` in Caddy. |
| Agent marked stale | It has not called `get_company_log_delta` recently. Add the per-turn call to its instructions. |
