<p align="center"><img src="docs/assets/banner.svg" alt="AI Computer Brain" width="100%"></p>

<h1 align="center">AI Computer Brain</h1>

<p align="center"><b>The shared memory that keeps every AI agent in your company in sync.</b></p>

<p align="center">
  <a href="#quick-install">Install</a> &middot;
  <a href="#the-terminal-console">Console</a> &middot;
  <a href="#user-guide">User guide</a> &middot;
  <a href="#connect-your-agents">Connect agents</a> &middot;
  <a href="docs/">Docs</a>
</p>

<p align="center">
  <img alt="MIT licence" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="MCP server" src="https://img.shields.io/badge/protocol-MCP-6f42c1">
  <img alt="Powered by Cognio Labs" src="https://img.shields.io/badge/powered%20by-Cognio%20Labs-38bdf8">
</p>

A self-hosted, open-source [MCP](https://modelcontextprotocol.io) server. Install it once on your PC or a VPS, create a project, hand each agent a key. From then on every agent that belongs to that project, wherever it runs, reads and writes the same living Markdown files and learns about every change on its next turn for a few dozen tokens.

Works with Hermes, OpenClaw, Claude Code, Cursor, custom Python agents, Slack or Discord bots, workflow tools, and anything else that can speak MCP or plain HTTPS. No LLM key is needed on the server. Agents bring their own model.

<table>
<tr><td><b>Terminal console</b></td><td><code>brain</code> opens a Hermes-style shell: pixel logo, slash commands, arrow-key menus, your shell commands still work inside it.</td></tr>
<tr><td><b>One brain, many agents</b></td><td>Every agent gets its own key and sees the same project. Hermes, OpenClaw, Claude Code, Cursor, Python, curl.</td></tr>
<tr><td><b>Token-cheap sync</b></td><td>Seven living Markdown files with heading-level etags. Agents read only what changed and log what they did.</td></tr>
<tr><td><b>Keys are identities</b></td><td><code>sb_&lt;project&gt;_&lt;role&gt;_&lt;agent&gt;_&lt;secret&gt;</code>, role-based permissions, approvals for sensitive sections.</td></tr>
<tr><td><b>Knowledge base</b></td><td>Upload PDFs, docs and sheets, share them per agent or role; with an LLM they get summaries and topics.</td></tr>
<tr><td><b>Fills itself</b></td><td>Connect the company repository and an LLM: the brain reads the code and writes the design system, stack, schema and pulse.</td></tr>
<tr><td><b>Own domain in one command</b></td><td><code>/domain brain.yourcompany.com</code> puts it on your domain from a laptop through Cloudflare, or on a VPS with HTTPS.</td></tr>
<tr><td><b>Always on</b></td><td><code>brain service install</code> keeps it running at login or boot, like <code>hermes gateway install</code>; every change is backed up to a private GitHub repository.</td></tr>
</table>

## Quick install

Linux, macOS and WSL:

```bash
curl -fsSL https://raw.githubusercontent.com/Cognio-so/AI_computer_brain/main/scripts/install.sh | bash
```

Windows PowerShell:

```powershell
iex (irm https://raw.githubusercontent.com/Cognio-so/AI_computer_brain/main/scripts/install.ps1)
```

That is the whole install. The one-liner installs `uv` (the Python manager) if it is missing, downloads Python 3.12 if needed, installs the brain as a tool and starts the console. No clone, no pipx, nothing to install first. Next time, type `brain`.

Already have `uv` or `pipx`? Then one command does the same:

```bash
uv tool install git+https://github.com/Cognio-so/AI_computer_brain     # or: pipx install git+https://github.com/Cognio-so/AI_computer_brain
brain
```

Once the package is on PyPI, `ai-computer-brain` works in place of the URL. Either way `brain` keeps everything in one folder it creates on first run, `~/.brain` (`C:\Users\<you>\.brain` on Windows), with `.env`, `data` and `brain_files` inside; set `BRAIN_HOME` to choose another one, on a laptop or a VPS.

From source, for development: `git clone https://github.com/Cognio-so/AI_computer_brain && cd AI_computer_brain && ./start.sh` (Windows: `start.cmd`). The script creates the environment, installs, and opens the console. `scripts/install.sh docker` sets up the Docker stack instead.

**First run.** `brain` asks for a colour theme, where the brain runs (this computer, a VPS, or this computer on your own domain through Cloudflare), whether to keep it running in the background, and creates your first project and key. Paste the printed command into your agent. Then `/new` and `/key` for more projects and agents, `/docs add` for the documents they must know, `/github` and `/llm` so the brain fills itself from your code, `/domain brain.yourcompany.com` to reach it from anywhere.

---

## The problem

Companies deploy specialised agents on different machines: Sales Hermes on VPS 1, Marketing Hermes on VPS 2, OpenClaw on a home server, Claude Code on a laptop, a reporting agent in the cloud. Each has its own memory. Sales changes a price and Marketing keeps writing the old one. The UI agent changes a colour token and the content agents keep using the previous palette. A schema migration lands and the analytics agent hallucinates column names. The usual fix, pasting the whole company manual into every prompt, burns most of the context window on text that did not change.

## The fix

One control plane, many isolated projects. Each project owns a Git-backed vault of seven canonical files:

| File | Holds |
|---|---|
| `DESIGN_SYSTEM.md` | Colours, type, components, tone of voice |
| `TECH_AND_INFRA.md` | Stack, services, endpoints, deployments |
| `DB_SCHEMA_STATE.md` | Tables and columns, kept current by a watcher |
| `COMPANY_LOG.md` | The last 50 changes by agents and humans, written by the server |
| `ACCESS_PERMISSIONS.md` | Who may read and write what, plus the rules served to agents |
| `COMPANY_PULSE.md` | Pricing, shipped features, priorities, open decisions |
| `AGENT_CHANGELOG.md` | What each department's agents did, dated and timed, plus the server-compiled Daily Digest |

Every `##` and `###` heading has an 8-character etag. Agents read a section only when its etag changed, ask "what changed since id N" at the start of every turn, and write back a one-line summary after they change something real. Each write is permission-checked, locked, logged, committed to Git and broadcast to the project's sync room.

Agents also share one changelog per project, for brain-level items only. After work that other departments must know about, an agent calls `log_agent_activity` with what it did, the kind (task, finding, decision, blocker) and the departments it affects; routine steps stay in the agent's own memory on the server it runs on. The server stamps the UTC date and time and writes the entry under that agent's own department section, never another department's. When design logs "button radius changed to 12px, affects: marketing", the marketing agent's next delta carries that entry flagged `affects_you`, so it knows to update the slides. Section writes get the same treatment automatically: departments whose watch list covers the changed heading are recorded as affected.

Compaction is the server's job, so no single agent decides what stays: an entry equal to one logged in the last 24 hours is not written twice, each department keeps its latest 100 entries with older ones archived under `logs/`, agents joining or being revoked are recorded automatically, findings are mirrored into the `Findings` section of `COMPANY_LOG.md`, and the Daily Digest lists everything that happened across all departments, day by day. The admin panel shows sync health per project: agents in sync or stale, the last GitHub event, and the last changelog entry.

---

## Setup and deployment

### The terminal console

`brain` works like Hermes or OpenClaw: the server runs in the background and the terminal becomes a shell with slash commands. It opens with the name drawn as pixel art in the colours of your theme and a status box:

```
  (AI COMPUTER BRAIN as a pixel logo in the theme colours)
 v0.1.0  the shared memory that keeps every agent in sync        powered by Cognio Labs

 Admin panel   http://127.0.0.1:8000/admin   /open
 MCP endpoint  http://127.0.0.1:8000/mcp
 Master key    bm_...dRa_   /master
 Projects      acme-corp (3 keys, 1 live, GitHub)   /projects
 LLM           not connected (optional)   /llm

brain @acme-corp> /help
```

Everything is a slash command, and anything without a slash runs in your shell (`ls`, `cat README.md`, `grep -n price brain_files`, `git status`, `cd docs`), so you never have to leave the console.

| Command | What it does |
|---|---|
| **Brain** | |
| `/new <company name>` | Creates the project, its ID, the first API key and prints the MCP connection; asks for the company repository and creates the `<project>_brain` backup repository on your GitHub |
| `/projects`, `/use <project>` | List the projects, choose the one the prompt works on |
| `/key <what it is for>` | Adds an API key for one more agent; role and agent ID are derived from the name |
| `/keys`, `/revoke`, `/agents` | Keys with their sync state, revoke one, who is connected right now |
| `/agent [agent id]` | An agent's key and connection commands at any time, and a replacement key |
| `/files`, `/show <FILE.md>`, `/upload <path> [FILE.md] [--smart]` | The brain files, print one, replace one from disk (`--smart` merges it with the LLM) |
| `/docs`, `/docs add <path> [--title t] [--agents a,b \| --roles r]`, `/docs share`, `/docs read`, `/docs rm` | Knowledge base: upload documents, choose which agents or roles may read them, read or remove one |
| `/chat [question]` | Ask the assistant about the brain, or tell it what to change in a brain file and approve its edits |
| `/backup`, `/backup account` | Create the backup repository later; set the GitHub account it is created under |
| `/delete [project]` | Delete a project in two steps, optionally with its backup repository |
| **Sync** | |
| `/activity`, `/follow` | Latest changes, or stream them live |
| `/github [off]`, `/analyze` | Connect the company repository; let the LLM read the code and fill the brain |
| `/llm [test\|off]` | Connect OpenAI, Anthropic, Gemini, OpenRouter, Bedrock or any OpenAI-compatible endpoint with a key or by signing in with your account; pick the model and the thinking effort |
| **Server** | |
| `/connect` | The exact command for each agent type with the current endpoint |
| `/tunnel` | A temporary public HTTPS URL right now (Cloudflare quick tunnel) |
| `/domain [name]`, `/domain vps`, `/domain hosting`, `/domain forget` | Your own domain or subdomain from this computer through Cloudflare; the steps for a VPS or a hosting platform; forget a saved domain |
| `/service [install\|status\|logs\|stop\|start\|restart\|uninstall]` | Run the brain in the background, starting at login or boot, kept running after the console closes |
| `/security`, `/protect`, `/master`, `/rotate` | Admin access: path, IP allowlist, lockout, master key |
| `/env`, `/set KEY value`, `/edit`, `/restart` | Settings in `.env` with secrets masked; change one and restart; open the file; restart the server or the service |
| **Console** | |
| `/help [command]`, `/status`, `/menu` | This list or one command in detail, the status box, a guided menu |
| `/theme`, `/open`, `/clear`, `/quit` | Colours, the panel in your browser, clear the screen, leave |

Lists are navigated with the arrow keys (PageUp, PageDown, Home, End, type to filter long lists) and Enter; Esc goes back. On a terminal without key support the same menus fall back to numbers.

The first run asks you to pick a colour theme (Ocean, Ember, Forest, Violet, Sunset, Mint, Gold or Mono, shown as live swatches), where the server will live (this computer; a VPS with a domain; or this computer on your own domain through Cloudflare), whether to run the brain in the background from now on, and whether to create the first project right away. Everything the console does is also in the admin panel, and `brain serve` runs the plain server for Docker or systemd with the same logo, a status box and quiet logs (`--verbose` for request logs).

Need remote agents to reach your PC? `/tunnel` in the console, or `brain serve --tunnel`, opens a Cloudflare quick tunnel and prints a public HTTPS URL (requires `cloudflared`).

### Your own domain or subdomain

`/domain brain.yourcompany.com` is a one-command Cloudflare deploy from this computer. It asks one thing, the name; checks that the domain is on Cloudflare (and shows click by click where to add it when it is not); shows the tunnel and the DNS record it is about to create; then downloads `cloudflared` if it is missing, signs you in to Cloudflare in the browser (once), creates the tunnel, adds the DNS record, starts the tunnel and waits until `https://brain.yourcompany.com` answers with your admin panel and MCP endpoint. Nothing to open on your router, HTTPS included; the domain must be managed by Cloudflare DNS (the free plan is enough), and the console tells you when it is not. When the domain is live the console offers to install the background service so the domain stays up after you close the console and after a reboot (`brain serve` starts the tunnel too when `CLOUDFLARE_TUNNEL` is set in `.env`).

For a server instead, `/domain vps` saves the domain and prints the DNS, firewall and `docker compose up -d` steps (Caddy obtains the certificate; always on, the right setup for a team), and `/domain hosting` prints the Railway, Render and Fly.io steps with the CNAME to add.

`/domain forget` removes a saved domain, after which the MCP endpoint is local again. The status box always shows the current state: no domain yet, a temporary tunnel URL, a saved domain that is not connected yet, or the domain with whether its tunnel is live.

### Keep it running in the background

Like `hermes gateway install`, `/service install` in the console (or `brain service install`) registers the brain as a background service that starts when you log in, restarts if it crashes and keeps running after you close the console. The console then attaches to the running service, and `/domain`, `/set` and the other commands restart the service instead of a console-owned server.

| Platform | What is installed | Starts |
|---|---|---|
| Linux | systemd user service `ai-computer-brain` (`sudo brain service install --system` for a boot-time service on a VPS) | at login; at boot with lingering or `--system` |
| macOS | launchd agent `com.cogniolabs.brain` with KeepAlive | at login |
| Windows | scheduled task "AI Computer Brain", hidden window | at logon |

`brain service status|logs|stop|start|restart|uninstall` manage it; logs land in `data/logs/brain.log`. The first-run wizard asks whether to install it.

### VPS with HTTPS

```bash
git clone https://github.com/Cognio-so/AI_computer_brain && cd AI_computer_brain
cp .env.example .env                  # set BRAIN_DOMAIN, PUBLIC_URL and ADMIN_MASTER_KEY
docker compose up -d                  # Caddy fetches the certificate automatically
```

Point a DNS `A` record for `brain.yourdomain.com` at the VPS first. The admin panel is at `https://brain.yourdomain.com/admin`. Step-by-step DNS, Cloudflare Tunnel and Cloudflare Access instructions are in [docs/cloudflare-domain.md](docs/cloudflare-domain.md).

### Railway, Render, Fly.io

Deploy the Dockerfile, attach a persistent volume at `/data` and `/app/brain_files`, set `ADMIN_MASTER_KEY` and `PUBLIC_URL`. The admin panel is served from the same process at `/admin`. See [docs/admin-panel-hosting.md](docs/admin-panel-hosting.md).

---

## Five-minute setup

1. Open `/admin`. The start script signs you in; otherwise paste the master key printed by the server or found in `.env`.
2. Type your company name and click **Create project**. The brain creates the project ID, the first API key and shows the MCP endpoint plus the exact command for your agent type (Claude Code, Hermes, OpenClaw, Cursor, Python, curl). Under **More options** you can add the company repository and let the brain create a private `<project>_brain` repository on your GitHub that receives every change to the brain files (it asks once for a GitHub token with the `repo` scope).
3. **Add agent** for every further agent: name it after its job ("Marketing Hermes on VPS 2") and the role and agent ID are derived from the name. Keys stay viewable: the **Connect an agent** card on the Overview shows any agent's endpoint, key and paste-ready commands, and **new key** replaces one (in the console, `/agent` does the same).
4. Paste the command into each agent. Done. The agents receive their operating rules through the MCP connection itself.
5. Optional: upload the documents your agents must know on the **Documents** page, connect the company repository and an LLM on **GitHub & LLM**, and the brain fills itself from the code.

Or from the terminal: `/new Acme Corp`, then `/key Sales Hermes on VPS 1`, or by script:

```bash
brain key quick "Sales Hermes on VPS 1" --project-name "Acme Corp"
brain key quick "Claude Code laptop"
```

---

## User guide

### Day one

1. **Start.** `./start.sh` or `brain`. The status box shows the admin panel, the MCP endpoint, the domain, where the brain is reachable from, whether it runs in the background, the master key (masked), the projects and the LLM.
2. **Create the project.** `/new Acme Corp` prints the project ID, the first key and the command for the agent. Say yes to the backup repository to have every change pushed to a private `acme-corp_brain` repository on your GitHub.
3. **More agents.** `/key Marketing Hermes on VPS 2`, one key per agent. `/keys` shows them with their sync state, `/revoke` retires one, `/agents` shows who is connected right now.
4. **Give the agents something to know.** `/upload design.md DESIGN_SYSTEM.md` replaces a brain file from disk (`--smart` merges with the LLM); `/docs add handbook.pdf --agents sales-hermes` adds a document to the knowledge base for chosen agents or roles.
5. **Let the brain fill itself.** `/llm` connects a model (key or account sign-in, then the model and the thinking effort), `/github` connects the company repository, `/analyze` reads the code into the right sections. With "analyse on every push" the brain keeps itself current.
6. **Watch and steer.** `/activity` and `/follow` show what agents change; `/chat` answers from the brain and proposes edits you approve; the admin panel (`/open`) adds approvals, rules and per-section editing.

### Reaching the brain from elsewhere

| You want | Do this |
|---|---|
| Agents on this computer only | Nothing. The MCP endpoint is `http://127.0.0.1:8000/mcp`. |
| A public URL right now, for a test | `/tunnel`. A temporary `trycloudflare.com` address that changes on every start. |
| Your own domain or subdomain from this computer | `/domain brain.yourcompany.com`. Cloudflare Tunnel, HTTPS, no open ports, free. Accept the background service so it stays up. |
| A VPS with HTTPS | `/domain vps` prints the DNS, firewall and `docker compose up -d` steps; see [VPS with HTTPS](#vps-with-https). |
| Railway, Render, Fly.io | `/domain hosting` prints the steps with the CNAME to add. |
| Forget a saved domain | `/domain forget`. The endpoint is local again. |

### Running in the background

`/service` installs the brain as a background service (Windows scheduled task, launchd agent or systemd service) so it starts at login or boot and keeps running after you close the console. The console then attaches to it: the status box says "running as a service", and `/restart`, `/set`, `/protect`, `/rotate` and `/domain` restart the service. `/service status`, `/service logs`, `/service stop`, `/service start` and `/service uninstall` manage it; `brain service ...` does the same from the shell.

### Protecting the admin panel

`/security` shows the current state; `/protect` moves the panel to an unguessable path, restricts it to your IPs and sets the lockout; `/master` shows the master key; `/rotate` replaces it. Agents never use the master key, only their own.

### Settings

`/env` lists the settings in `.env` with secrets masked, `/set KEY value` changes one and restarts, `/edit` opens the file in your editor, `/restart` restarts the server or the service, `/theme` changes the colours.

### Updating

```bash
git pull
brain service restart       # or /restart in the console; Docker: docker compose up -d --build
```

Schema changes apply themselves; brain files and documents are untouched. The console warns when the code on disk is newer than the running console: `/quit` and start it again.

### When something is off

| Symptom | Check |
|---|---|
| The console says the server did not start | Another brain is already on the port, usually the background service. Start `brain` again to attach, or `brain service stop`. |
| The domain answers 404 | A tunnel other than the brain's is bound to the name: stop other `cloudflared` processes, delete the old tunnel under Zero Trust → Networks → Tunnels, run `/domain` again. |
| The status box says "saved but not connected" | `.env` holds a domain that was never deployed from this computer or points elsewhere: `/domain` deploys it, `/domain vps` prints the server steps, `/domain forget` removes it. |
| Agents cannot reach the brain | `brain doctor` checks git, cloudflared, the background service, the database and the public URL. |
| The service is installed but not answering | `brain service logs` shows `data/logs/brain.log`; `brain service install` refreshes the definition after Python or the folder moved. |

---

## Connect your agents

Every agent needs two values: the server URL and its key. The key has the shape `sb_<project>_<role>_<agent>_<secret>` and binds the agent to one project and one role. It is the agent's identity and password in one: the agent sends it as a Bearer token on every MCP request, the brain reads the project, the role and the agent from it, and nothing else ever references it. The Overview's Connect an agent card, the Agents page and `/agent` show an agent's key and commands at any time; new key replaces one.

**Hermes** (`~/.hermes/config.yaml`)

```yaml
mcp_servers:
  company-brain:
    url: https://brain.yourdomain.com/mcp
    headers:
      Authorization: "Bearer sb_acme-corp_sales_hermes-sales-01_..."
```

**OpenClaw** (mcporter)

```bash
mcporter config add company-brain --url https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_ops_openclaw-home_..."
```

**Claude Code**

```bash
claude mcp add --transport http company-brain https://brain.yourdomain.com/mcp \
  --header "Authorization: Bearer sb_acme-corp_engineering_claude-code-01_..."
```

**Cursor, Windsurf and other `mcp.json` clients**

```json
{ "mcpServers": { "company-brain": {
    "url": "https://brain.yourdomain.com/mcp",
    "headers": { "Authorization": "Bearer sb_acme-corp_marketing_hermes-marketing-01_..." } } } }
```

**Python agent**

```python
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

http = create_mcp_http_client(headers={"Authorization": "Bearer sb_acme-corp_engineering_analytics-01_..."})
async with streamable_http_client("https://brain.yourdomain.com/mcp", http_client=http) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        delta = await session.call_tool("get_company_log_delta", {"since_entry_id": 0})
```

**Anything that speaks HTTPS**

```bash
curl -N -H "Authorization: Bearer sb_..." https://brain.yourdomain.com/events
```

Legacy SSE clients connect to `/sse`. Serena is itself a tool server, so add the brain to the host that runs Serena, such as Claude Code. More hosts and troubleshooting in [docs/connecting-agents.md](docs/connecting-agents.md).

---

## How an agent uses the brain

A new project starts empty on purpose: every section of the four content files holds a single placeholder line such as `- (no pricing documented yet)` instead of sample data, and the rules tell agents that such a section carries no facts. Nothing an agent reads is invented; the files fill up from the admin, the agents, the uploads and the repository analysis. The key card in the panel and in `/new` ends with a short "What the agent does once connected" guide, and `whoami.sources` tells the agent where the backup repository and the company repository live.

The rules travel inside the connection: the MCP `instructions` field, the tool descriptions, the `whoami` response and a sync banner on every result. Operators configure only the URL and the key.

1. **Session start.** `whoami`, then `get_brain_index`, then read the sections the role depends on and keep their etags.
2. **Every turn.** `get_company_log_delta(since_entry_id=<last id>)`. Zero entries when nothing changed. Re-read only sections that did. Entries marked `affects_you` are work for the agent: adjust, then log the adjustment.
3. **After changing something real.** `update_brain_section` with a one-line summary for the other agents and the etag from the last read. On `CONFLICT`, re-read the returned current content, merge, retry once.
4. **After every completed task.** `log_agent_activity` with the summary, kind and affected departments. Before finishing a session, log the day's findings.

Tools:

| Tool | Purpose |
|---|---|
| `whoami` | Role, permissions, rules, file guide, latest log id |
| `get_brain_index` | Every heading of every file with its etag, about 150 tokens |
| `read_brain_section` | One heading; `NOT_MODIFIED` when the etag matches |
| `read_brain_file` | A whole file with its section index |
| `update_brain_section` | Replace, append, create or delete one heading |
| `get_company_log_delta` | What changed since an id, filtered to the agent's watch list and anything marked as affecting its department |
| `log_agent_activity` | Record a completed task, finding, decision or blocker in the agent's own changelog section, with the departments it affects |
| `get_daily_digest` | Everything that happened on one day across all departments |
| `list_documents`, `search_documents`, `read_document` | The knowledge base: documents the admin uploaded and shared with this agent, searched and read in chunks |
| `sync_db_schema` | Rewrite only the tables that changed in `DB_SCHEMA_STATE.md` |
| `get_proposal` | Outcome of a write that needed human approval |

Agents only see the tools their role may use.

---

## Permissions and approvals

`ACCESS_PERMISSIONS.md` is both the documentation and the policy:

```markdown
| File               | admin | engineering | sales | marketing | ops | design |
|--------------------|-------|-------------|-------|-----------|-----|--------|
| COMPANY_PULSE.md   | rw    | r           | rw    | rw        | rw  | r      |
```

Values are `r`, `rw`, `rw-approve` and `-`. Heading overrides win over the file matrix. With `rw-approve`, a write becomes a proposal: the agent gets `PENDING_APPROVAL`, an admin approves or rejects it in the panel, and the approved change is applied, logged, committed and broadcast. The same file holds the `Agent Rules`, `File Guide` and `Sync Policy` served to agents, editable from the **Rules** tab.

## Documents: the agents' knowledge base

Upload the documents your agents must know (SOPs, specs, contracts, research, price lists) on the **Documents** page or with `/docs add <file>`. Markdown, text, CSV, JSON, YAML, HTML, PDF and Word files are accepted; the text is extracted and stored next to the brain files in `docs/`. With an LLM connected every upload is indexed in the background: a short summary, topics and the departments it concerns, shown in the panel and handed to agents in `whoami` and `list_documents`, so an agent knows which document to open before reading a single page, and `search_documents` matches topics as well as text. For each document you choose who may read it: every agent of the project, specific agents, or every agent with a role such as sales. Agents see the ones shared with them in `whoami` and through `list_documents`, find passages with `search_documents` and read them in chunks with `read_document`, so a 200-page handbook never floods their context. Uploads are logged and flagged to the affected departments like any other change.

## GitHub and the optional LLM

Connect the company repository on **GitHub & LLM** (or `/github`): URL, branch and, for private repositories, an access token. The page shows the webhook payload URL and secret to paste into GitHub so every push lands in the company log.

Connect an LLM provider on the same page (or `/llm`): OpenAI, Anthropic, Google Gemini, OpenRouter, Amazon Bedrock, or any OpenAI-compatible endpoint such as Ollama, Groq or Mistral. Enter the key, the brain checks it and lists the models that key can use, you pick one, and then the thinking effort (low, medium, high; Anthropic also xhigh and max) that the server uses for analysis, uploads and the assistant. The key stays in `.env` on your server. You can also skip API keys and sign in with your account, the way Hermes, Claude Code, the Codex CLI and the Gemini CLI do: pick **Sign in with your account**, a browser tab opens, you sign in, and the brain receives the code (through a local callback when the browser runs on the same machine, otherwise you paste the code or the address back). Claude accounts, ChatGPT accounts and Google accounts are supported; the tokens stay in `data/oauth.json` on your server and refresh themselves. These flows use the public OAuth clients of those tools, so they may change when the vendors change them; an API key always works as the fallback. Anthropic additionally accepts the official CLI profile from `ant auth login`. With a model connected, **Analyse now** (or `/analyze`) clones the repository, reads the manifests, Dockerfiles, migrations, ORM models, design tokens, routes, docs and recent commits, and writes what it learns into the right sections: brand colours and typography into `DESIGN_SYSTEM.md`, stack, services and deployments into `TECH_AND_INFRA.md`, tables into `DB_SCHEMA_STATE.md`, positioning, pricing and priorities into `COMPANY_PULSE.md`, and cross-department findings into the changelog and company log. Only sections with real evidence change; unchanged sections are left alone, secrets are redacted, and every write is logged and broadcast like an agent's. The first analysis starts by itself the moment the repository is connected while an LLM is on, and with **Analyse on every push** the brain keeps itself current from the code even while no agent is working. The same model powers `--smart` uploads, which merge a document into the right headings of a brain file. Agents never need the LLM: they bring their own model.

## Assistant

With an LLM connected, the **Assistant** page (or `/chat` in the console) answers questions from the brain files, the findings, the recent activity and the documents shared in the project, citing the file and heading it used. Ask it to change something ("add a Team plan at $99 per month", "the primary colour is now #2563EB", "document the refund window") and it proposes the exact edits to the right sections; you review them and apply them with one click, after which they are logged, committed and broadcast to the agents like any other write. The company log and the agent changelog stay server-written.

## Backup repository and disk edits

Each project can own a private `<project>_brain` repository on your GitHub, created with one click (or at project creation, or with `/backup`). Every write to the brain files, whether by an agent, the panel, the assistant, an upload or the repository analysis, is committed and pushed there within seconds, so the repository is a complete, versioned backup you can clone anywhere. Files edited directly on disk are picked up by a watcher (every 60 seconds by default, `DISK_WATCH_SECONDS`), committed, logged as a disk edit, broadcast to the agents and pushed as well. Deleting a project is a two-step action from the Delete button; the backup repository stays on GitHub unless you tick the option to remove it.

## Keeping humans in the loop

- **GitHub webhook.** Settings → generate webhook secret, paste the payload URL into your repository. Developer pushes appear in the company log. If the project's Git remote is that repository, the vault is pulled and every changed section is rebroadcast.
- **Database watcher.** `brain db-watch --dsn postgresql://ro:***@db/app --key sb_...engineering...` runs beside your database and updates `DB_SCHEMA_STATE.md` whenever the schema changes.
- **Admin panel.** Edit any file or section in the browser. Your edits are broadcast like any agent write.

## Admin panel

`/admin`, served by the same process, in the colours of your theme with a dark and a light mode. Overview with sync health and sources, agents with one-time key reveal and config snippets, the brain files with a rendered viewer, per-section editing, conflict detection, upload and download, the documents knowledge base with per-agent sharing, a live activity stream with per-change diffs, the approvals queue, the rules editor, GitHub and LLM connection with live analysis progress, and project settings with vault Git remote, webhook secret, export and import.

The panel is a single static file. It can also be hosted on another origin such as Cloudflare Pages, pointed at the server through its login screen, with `CORS_ORIGINS` set on the server.

## Configuration

All settings are environment variables, see [`.env.example`](.env.example). The ones you will touch:

| Variable | Meaning |
|---|---|
| `BRAIN_HOME` | The folder holding `.env`, `data` and `brain_files`: `~/.brain` by default, or the current folder when it holds a `.env`. An environment variable, not an `.env` entry. |
| `ADMIN_MASTER_KEY` | Unlocks the admin panel and the admin API. `brain init` generates it. |
| `ADMIN_PATH` | Where the panel is served, `/admin` by default. Pick something unguessable on a public host. |
| `ADMIN_ALLOWED_IPS` | Comma-separated IPs or CIDR ranges allowed to reach the panel and admin API. Empty allows all. |
| `PUBLIC_URL` | The URL agents use. Appears in generated snippets. |
| `BRAIN_DOMAIN` | Hostname Caddy serves with automatic HTTPS. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/brain_metadata.db` by default; any `postgresql://...` for PostgreSQL. |
| `REDIS_URL` | Optional. Required only for more than one worker. |
| `BRAIN_FILES_DIR` | Where project vaults live. Each vault is a Git repository. |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_AUTH` | Optional model used by the server itself for the assistant, repository analysis and smart uploads; set from the panel or `/llm`. `LLM_AUTH=oauth` uses the account sign-in, `login` the Anthropic CLI profile. |
| `GITHUB_TOKEN`, `GITHUB_OWNER` | GitHub account used to create and push the `<project>_brain` backup repositories; set from the panel or `/backup account`. |
| `DISK_WATCH_SECONDS` | How often files edited directly on disk are committed, logged and pushed (0 disables). |
| `CORS_ORIGINS` | Comma-separated origins allowed to call the admin API from another domain. |

### PostgreSQL instead of SQLite

Install PostgreSQL on the VPS or run it as a container, create a database, set `DATABASE_URL=postgresql://brain:password@host:5432/second_brain`, restart. The schema applies itself on startup.

### Backups

Each vault has a Git history that the brain keeps in `brain_files/.git-store/<project>`, so the vault folder itself is plain Markdown and editors do not see it as a repository; create the backup repository or set a remote in project settings and every change is pushed. Metadata lives in the `brain_data` volume or `./data`; back it up with `sqlite3 .backup` or `pg_dump`. `brain vault export <project> --out vault.zip` produces a portable archive.

## Security notes

Keys are verified by their SHA-256 hash; a copy sealed with the master key (Fernet, key derived with PBKDF2) is kept so the panel and the console can show a key again. Without the master key the copies are unreadable, and rotating the master key re-seals them. Project isolation comes from the key, never from tool arguments. File names are checked against the seven canonical names. Permissions fail closed. Webhooks require a valid HMAC signature. Writes are rate limited, size capped, and refused when the content looks like a credential. The company log is written only by the server, and each agent can only ever write its own section of the changelog. Risky features are off until configured: Git push needs a remote, webhooks need a secret, approvals need an `rw-approve` entry, the tunnel needs `--tunnel`.

The admin panel is reachable at a public URL only in the sense that the page loads. Every admin API call needs the master key and answers 401 without it, so knowing `brain.yourdomain.com` gives nobody access. Three settings tighten it further: `ADMIN_PATH` moves the panel to a non-guessable path such as `/admin-7f3a9c`, `ADMIN_ALLOWED_IPS` restricts the panel and the admin API to your office or VPN ranges, and `ADMIN_LOGIN_FAILURES_PER_MINUTE` locks an address out after repeated wrong keys. Cloudflare Access or a Caddy IP allowlist add a layer on top, see [docs/admin-panel-hosting.md](docs/admin-panel-hosting.md).

## CLI

Everything the console does is also a plain command, for scripts and headless servers.

| Command | What it does |
|---|---|
| `brain` | The console; the first run is the setup wizard |
| `brain init [dir] [--public-url ...]` | Create `.env` with a master key without the wizard |
| `brain serve [--tunnel] [--open] [--verbose] [--no-wizard]` | The plain server; starts the named tunnel when `CLOUDFLARE_TUNNEL` is set |
| `brain service install [--system] \| start \| stop \| restart \| status \| logs \| uninstall` | The background service |
| `brain project create <id> --name "..." \| list \| delete <id>` | Projects |
| `brain key quick "<what it is for>" [--project-name "..."]` | A key (and the project if needed) from a plain name |
| `brain key create --project <id> --role <role> --agent <agent id> \| list --project <id> \| revoke <key id> --project <id>` | Keys |
| `brain vault export <id> --out vault.zip \| import <id> vault.zip` | Portable archive of a project vault |
| `brain log tail --project <id>` | Follow the activity stream |
| `brain db-watch --dsn <url> --project <id> --key <sb_...>` | Keep `DB_SCHEMA_STATE.md` current from a live database |
| `brain doctor` | Checks git, cloudflared, the background service, templates, database and public URL |

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
pytest
```

The test suite covers the Markdown engine, RBAC, keys, the service layer, the console and its logo, the LLM providers, the repository analyser against a real Git repository with a stubbed model, the documents store, the assistant and the Anthropic sign-in flow, and an end-to-end run that boots the server and drives it with a real MCP client.

## License

MIT. See [LICENSE](LICENSE).
