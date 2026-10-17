import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

MASTER = "bm_e2e-master-key-0123456789abcdefghij"
ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "BRAIN_FILES_DIR": str(workdir / "brain_files"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(workdir / 'meta.db').as_posix()}",
        "ADMIN_MASTER_KEY": MASTER,
        "PUBLIC_URL": base,
        "GIT_COMMIT_DEBOUNCE_SECONDS": "0",
        "LOG_LEVEL": "warning",
        "CORS_ORIGINS": "",
        "REDIS_URL": "",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "second_brain.server:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(workdir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 40
    ready = False
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    if not ready:
        output = process.stdout.read() if process.stdout else ""
        process.kill()
        raise RuntimeError(f"server did not start:\n{output}")
    try:
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def admin(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {MASTER}"}, timeout=30)


async def call(base: str, key: str, tool: str, arguments: dict | None = None) -> dict:
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with streamable_http_client(f"{base}/mcp", http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments or {})
            if result.structured_content:
                return dict(result.structured_content)
            return json.loads(result.content[0].text)


async def tools_for(base: str, key: str) -> list[str]:
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with streamable_http_client(f"{base}/mcp", http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert "get_company_log_delta" in (init.instructions or "")
            listed = await session.list_tools()
            return sorted(t.name for t in listed.tools)


def test_admin_flow_and_mcp_round_trip(server):
    page = httpx.get(f"{server}/admin")
    assert page.status_code == 200 and "brainAdmin()" in page.text
    assert httpx.get(f"{server}/", follow_redirects=False).status_code == 307
    with admin(server) as client:
        assert client.get("/api/admin/status").json()["setup_required"] is True
        assert httpx.get(f"{server}/api/admin/status").status_code == 401
        created = client.post(
            "/api/admin/projects", json={"project_id": "acme-corp", "project_name": "Acme Corp"}
        )
        assert created.status_code == 201, created.text
        roles = client.get("/api/admin/projects/acme-corp/roles").json()
        assert "sales" in roles and "engineering" in roles
        sales = client.post(
            "/api/admin/projects/acme-corp/keys",
            json={
                "department_role": "sales",
                "agent_id": "hermes-sales-01",
                "label": "Sales Hermes",
            },
        ).json()
        marketing = client.post(
            "/api/admin/projects/acme-corp/keys",
            json={"department_role": "marketing", "agent_id": "hermes-marketing-01"},
        ).json()
        engineering = client.post(
            "/api/admin/projects/acme-corp/keys",
            json={"department_role": "engineering", "agent_id": "claude-code-01"},
        ).json()
        assert sales["plaintext"].startswith("sb_acme-corp_sales_hermes-sales-01_")
        assert "claude mcp add" in sales["snippets"]["claude_code"]

    sales_key, marketing_key, engineering_key = (
        sales["plaintext"],
        marketing["plaintext"],
        engineering["plaintext"],
    )

    assert httpx.post(f"{server}/mcp", json={}).status_code == 401
    assert "sync_db_schema" not in asyncio.run(tools_for(server, sales_key))
    assert "sync_db_schema" in asyncio.run(tools_for(server, engineering_key))

    me = asyncio.run(call(server, sales_key, "whoami"))
    assert me["role"] == "sales" and me["permissions"]["COMPANY_PULSE.md"] == "rw"
    index = asyncio.run(call(server, sales_key, "get_brain_index"))
    pulse = next(f for f in index["files"] if f["file"] == "COMPANY_PULSE.md")
    pricing = next(s for s in pulse["sections"] if s["slug"] == "pricing-tiers")

    marketing_delta = asyncio.run(
        call(server, marketing_key, "get_company_log_delta", {"since_entry_id": 0})
    )
    since = marketing_delta["latest_id"]

    written = asyncio.run(
        call(
            server,
            sales_key,
            "update_brain_section",
            {
                "file_name": "COMPANY_PULSE.md",
                "heading": "Pricing Tiers",
                "patch_content": "| Plan | Price |\n|---|---|\n| Pro | $49 / month |",
                "compact_summary": "Pro tier $39→$49/mo; annual discount 20%",
                "expected_etag": pricing["etag"],
            },
        )
    )
    assert written["status"] == "ok" and written["new_etag"] != pricing["etag"]

    stale = asyncio.run(
        call(
            server,
            sales_key,
            "update_brain_section",
            {
                "file_name": "COMPANY_PULSE.md",
                "heading": "Pricing Tiers",
                "patch_content": "| Pro | $59 |",
                "compact_summary": "stale write",
                "expected_etag": pricing["etag"],
            },
        )
    )
    assert stale["status"] == "CONFLICT" and "$49" in stale["current_content"]

    read = asyncio.run(
        call(
            server,
            marketing_key,
            "read_brain_section",
            {
                "file_name": "COMPANY_PULSE.md",
                "heading": "pricing-tiers",
                "known_etag": pricing["etag"],
            },
        )
    )
    assert read["status"] == "ok" and "$49" in read["content"]
    assert "sync" in read and read["sync"]["unread"] >= 1
    not_modified = asyncio.run(
        call(
            server,
            marketing_key,
            "read_brain_section",
            {
                "file_name": "COMPANY_PULSE.md",
                "heading": "pricing-tiers",
                "known_etag": read["etag"],
            },
        )
    )
    assert not_modified["status"] == "NOT_MODIFIED"

    delta = asyncio.run(
        call(server, marketing_key, "get_company_log_delta", {"since_entry_id": since})
    )
    assert any(e["summary"].startswith("Pro tier $39") for e in delta["entries"])

    forbidden = asyncio.run(
        call(
            server,
            engineering_key,
            "update_brain_section",
            {
                "file_name": "COMPANY_PULSE.md",
                "heading": "Pricing Tiers",
                "patch_content": "x",
                "compact_summary": "engineering edits pricing",
            },
        )
    )
    assert forbidden["status"] == "FORBIDDEN"

    schema = asyncio.run(
        call(
            server,
            engineering_key,
            "sync_db_schema",
            {
                "schema_payload": {
                    "tables": [
                        {
                            "name": "orders",
                            "columns": [{"name": "id", "type": "bigint", "nullable": False}],
                        }
                    ]
                },
                "compact_summary": "orders table added",
            },
        )
    )
    assert schema["status"] == "ok" and schema["changed_tables"] == ["orders"]

    with admin(server) as client:
        files = client.get("/api/admin/projects/acme-corp/files").json()
        assert len(files) == 7
        doc = client.get("/api/admin/projects/acme-corp/files/COMPANY_PULSE.md").json()
        assert "$49" in doc["content"]
        section = client.get(
            "/api/admin/projects/acme-corp/files/COMPANY_PULSE.md/sections/open-decisions"
        ).json()
        saved = client.put(
            "/api/admin/projects/acme-corp/files/COMPANY_PULSE.md/sections/open-decisions",
            json={
                "content": "- Approve Q4 budget",
                "compact_summary": "Budget decision opened",
                "mode": "replace",
            },
            headers={"If-Match": section["etag"]},
        )
        assert saved.status_code == 200, saved.text
        conflict = client.put(
            "/api/admin/projects/acme-corp/files/COMPANY_PULSE.md/sections/open-decisions",
            json={"content": "- stale", "compact_summary": "stale", "mode": "replace"},
            headers={"If-Match": section["etag"]},
        )
        assert conflict.status_code == 409
        log = client.get("/api/admin/projects/acme-corp/log/recent?limit=20").json()
        assert any(e["summary"] == "Budget decision opened" for e in log)
        time.sleep(1.5)
        committed = next(
            e
            for e in client.get("/api/admin/projects/acme-corp/log/recent?limit=50").json()
            if e["summary"].startswith("Pro tier $39")
        )
        assert committed["commit_sha"]
        diff = client.get(f"/api/admin/projects/acme-corp/log/{committed['log_id']}/diff").json()
        assert "$49" in diff["diff"]
        exported = client.get("/api/admin/projects/acme-corp/export")
        assert exported.status_code == 200 and exported.headers["content-type"] == "application/zip"
        keys = client.get("/api/admin/projects/acme-corp/keys").json()
        assert len(keys) == 3
        revoked = client.delete(f"/api/admin/projects/acme-corp/keys/{keys[0]['key_id']}")
        assert revoked.status_code == 200 and revoked.json()["active"] is False


def test_events_stream_and_webhook(server):
    with admin(server) as client:
        client.post(
            "/api/admin/projects", json={"project_id": "beta-fintech", "project_name": "Beta"}
        )
        ops = client.post(
            "/api/admin/projects/beta-fintech/keys",
            json={"department_role": "ops", "agent_id": "openclaw-home"},
        ).json()["plaintext"]
        secret = client.post("/api/admin/projects/beta-fintech/webhook-secret").json()[
            "webhook_secret"
        ]

    with httpx.stream(
        "GET", f"{server}/events", headers={"Authorization": f"Bearer {ops}"}, timeout=20
    ) as stream:
        lines = stream.iter_lines()
        first = next(line for line in lines if line.startswith("event:"))
        assert first == "event: ready"
        asyncio.run(
            call(
                server,
                ops,
                "update_brain_section",
                {
                    "file_name": "COMPANY_PULSE.md",
                    "heading": "Current Priorities",
                    "patch_content": "1. Ship billing",
                    "compact_summary": "Priorities set",
                },
            )
        )
        event = next(line for line in lines if line.startswith("event:") and line != "event: ready")
        assert event == "event: section_updated"

    import hashlib
    import hmac

    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc1234def",
            "repository": {"full_name": "beta/app", "clone_url": "https://github.com/beta/app.git"},
            "commits": [
                {
                    "id": "abc1234def5678",
                    "message": "Add Stripe webhook handler\n\nmore",
                    "author": {"name": "alice"},
                    "added": ["a.py"],
                    "modified": ["b.py"],
                    "removed": [],
                }
            ],
        }
    ).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    bad = httpx.post(
        f"{server}/webhooks/beta-fintech/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert bad.status_code == 401
    good = httpx.post(
        f"{server}/webhooks/beta-fintech/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "d1",
            "Content-Type": "application/json",
        },
    )
    assert good.status_code == 200 and good.json()["commits_logged"] == 1
    duplicate = httpx.post(
        f"{server}/webhooks/beta-fintech/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "d1",
            "Content-Type": "application/json",
        },
    )
    assert duplicate.json().get("duplicate") is True
    with admin(server) as client:
        log = client.get("/api/admin/projects/beta-fintech/log/recent?limit=10").json()
        assert any(e["type"] == "human_commit" and "alice" in e["summary"] for e in log)
        hot = client.get("/api/admin/projects/beta-fintech/files/COMPANY_LOG.md").json()["content"]
        assert "alice" in hot


def test_admin_client_used_by_the_shell(server):
    from second_brain.tui import AdminClient

    client = AdminClient(server, MASTER)
    try:
        assert client.status()["ok"] is True
        created = client.quick_key("Shell Co", project_name="Shell Co")
        assert created["project"]["project_id"] == "shell-co" and created["role"] == "admin"
        second = client.quick_key("Marketing bot", project_id="shell-co")
        assert second["role"] == "marketing"
        keys = client.keys("shell-co")
        assert len(keys) == 2
        assert client.revoke("shell-co", keys[0]["key_id"])["active"] is False
        assert any(e["type"] == "agent_joined" for e in client.recent("shell-co", 10))
        secret = client.webhook_secret("shell-co")
        assert secret["webhook_url"].endswith("/webhooks/shell-co/github")
        assert (
            client.update_project("shell-co", project_name="Shell Company")["project_name"]
            == "Shell Company"
        )
    finally:
        client.close()


def test_quick_project_documents_llm_and_github_endpoints(server):
    with admin(server) as client:
        created = client.post(
            "/api/admin/projects/quick",
            json={"name": "Northwind Traders", "first_agent": "Sales desk"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        pid = body["project"]["project_id"]
        assert pid == "northwind-traders" and body["agent_id"] == "sales-desk"
        assert body["plaintext"].startswith(f"sb_{pid}_sales_sales-desk_")
        assert "claude mcp add" in body["command"] and body["project"]["key_count"] == 1
        again = client.post("/api/admin/projects/quick", json={"name": "Northwind Traders"})
        assert again.json()["project"]["project_id"] == "northwind-traders-02"
        extra = client.post("/api/admin/keys/quick", json={"name": "Ops bot", "project_id": pid})
        extra_id = extra.json()["key"]["key_id"]
        info = client.get(f"/api/admin/projects/{pid}/keys/{extra_id}/connect")
        assert info.status_code == 200 and info.json()["mcp_url"].endswith("/mcp")
        assert info.json()["plaintext"] == extra.json()["plaintext"]
        assert extra.json()["plaintext"] in info.json()["snippets"]["claude_code"]
        fresh = client.post(f"/api/admin/projects/{pid}/keys/{extra_id}/reissue")
        assert fresh.status_code == 201 and fresh.json()["plaintext"] != extra.json()["plaintext"]
        assert fresh.json()["agent_id"] == "ops-bot" and fresh.json()["replaced_key_id"] == extra_id
        states = {
            k["key_id"]: k["active"] for k in client.get(f"/api/admin/projects/{pid}/keys").json()
        }
        assert states[extra_id] is False and states[fresh.json()["key"]["key_id"]] is True
        assert client.get(f"/api/admin/projects/{pid}/keys/nope/connect").status_code == 404

        doc = client.post(
            f"/api/admin/projects/{pid}/documents",
            files={
                "upload": (
                    "sales-playbook.md",
                    b"# Playbook\n\nAlways quote annual pricing first.\n",
                    "text/markdown",
                )
            },
            data={"title": "Sales playbook", "access": "sales-desk"},
        )
        assert doc.status_code == 201, doc.text
        doc_id = doc.json()["doc_id"]
        assert doc.json()["access"] == ["sales-desk"] and doc.json()["text_chars"] > 10
        listed = client.get(f"/api/admin/projects/{pid}/documents").json()
        assert [d["doc_id"] for d in listed] == [doc_id]
        detail = client.get(f"/api/admin/projects/{pid}/documents/{doc_id}").json()
        assert "annual pricing" in detail["preview"]
        shared = client.put(
            f"/api/admin/projects/{pid}/documents/{doc_id}",
            json={"access": ["*"], "title": "Playbook"},
        ).json()
        assert shared["shared_with_all"] is True and shared["title"] == "Playbook"
        download = client.get(f"/api/admin/projects/{pid}/documents/{doc_id}/download")
        assert download.status_code == 200 and b"Always quote" in download.content
        rejected = client.post(
            f"/api/admin/projects/{pid}/documents",
            files={"upload": ("archive.zip", b"PK\x03\x04", "application/zip")},
        )
        assert rejected.status_code == 415

        key = body["plaintext"]
        docs = asyncio.run(call(server, key, "list_documents"))
        assert [d["doc_id"] for d in docs["documents"]] == [doc_id]
        chunk = asyncio.run(
            call(server, key, "read_document", {"doc_id": doc_id, "max_chars": 300})
        )
        assert "annual pricing" in chunk["content"] and chunk["next_offset"] is None
        found = asyncio.run(call(server, key, "search_documents", {"query": "annual pricing"}))
        assert found["hits"][0]["doc_id"] == doc_id
        missing = asyncio.run(call(server, key, "read_document", {"doc_id": "nope"}))
        assert missing["status"] == "ERROR" and missing["code"] == 404
        assert "read_document" in asyncio.run(tools_for(server, key))
        assert client.delete(f"/api/admin/projects/{pid}/documents/{doc_id}").json() == {
            "deleted": doc_id
        }

        llm = client.get("/api/admin/llm").json()
        assert llm["enabled"] is False and len(llm["providers"]) == 6
        saved = client.put(
            "/api/admin/llm",
            json={
                "provider": "openrouter",
                "api_key": "sk-or-test-1234567890",
                "model": "",
                "effort": "high",
            },
        ).json()
        assert saved["enabled"] is True and saved["model"] == "anthropic/claude-opus-5.5"
        assert saved["effort"] == "high" and "xhigh" not in saved["effort_options"]
        assert saved["key_hint"].startswith("sk-or-")
        assert "sk-or-test-1234567890" not in json.dumps(saved)
        assert client.get("/api/admin/status").json()["llm"]["provider"] == "openrouter"
        assert client.put("/api/admin/llm", json={"provider": "bard"}).status_code == 400

        github = client.get(f"/api/admin/projects/{pid}/github").json()
        assert github["connected"] is False and github["llm_enabled"] is True
        connected = client.put(
            f"/api/admin/projects/{pid}/github",
            json={
                "repo_url": "https://github.com/acme/northwind",
                "token": "ghp_secret",
                "branch": "main",
            },
        ).json()
        assert connected["connected"] and connected["token_set"] and connected["webhook_secret"]
        assert connected["webhook_url"].endswith(f"/webhooks/{pid}/github")
        overview = client.get(f"/api/admin/projects/{pid}").json()
        assert overview["github_connected"] is True and "ghp_secret" not in json.dumps(overview)
        junk = client.put(f"/api/admin/projects/{pid}/github", json={"repo_url": "junk"})
        assert junk.status_code == 400
        assert client.post(f"/api/admin/projects/{pid}/github/analyze").status_code == 202
        deadline = time.time() + 30
        state = {}
        while time.time() < deadline:
            state = client.get(f"/api/admin/projects/{pid}/github/analysis").json()
            if state["state"] in ("done", "failed"):
                break
            time.sleep(0.3)
        assert state["state"] == "failed" and state["error"]
        assert client.delete(f"/api/admin/projects/{pid}/github").json()["connected"] is False
        client.put("/api/admin/llm", json={"provider": ""})
        assert client.post(f"/api/admin/projects/{pid}/github/analyze").status_code == 409

        upload = client.post(
            f"/api/admin/projects/{pid}/files/DESIGN_SYSTEM.md/upload",
            files={
                "upload": (
                    "design.md",
                    b"# Design\n\n## Brand Colors\n\n- primary #112233\n",
                    "text/markdown",
                )
            },
            data={"mode": "replace"},
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["mode"] == "replace"
        content = client.get(f"/api/admin/projects/{pid}/files/DESIGN_SYSTEM.md").json()
        assert "#112233" in content["content"]
        bad = client.post(
            f"/api/admin/projects/{pid}/files/DESIGN_SYSTEM.md/upload",
            files={"upload": ("flat.md", b"no headings here", "text/markdown")},
        )
        assert bad.status_code == 400
        smart = client.post(
            f"/api/admin/projects/{pid}/files/DESIGN_SYSTEM.md/upload",
            files={"upload": ("d.md", b"## X\n\nhi\n", "text/markdown")},
            data={"mode": "smart"},
        )
        assert smart.status_code == 409


def test_changelog_digest_and_admin_throttle(server):
    with admin(server) as client:
        client.post(
            "/api/admin/projects", json={"project_id": "gamma-shop", "project_name": "Gamma"}
        )
        design = client.post(
            "/api/admin/projects/gamma-shop/keys",
            json={"department_role": "design", "agent_id": "hermes-ui-01"},
        ).json()["plaintext"]
        marketing = client.post(
            "/api/admin/projects/gamma-shop/keys",
            json={"department_role": "marketing", "agent_id": "hermes-marketing-01"},
        ).json()["plaintext"]
    tools = asyncio.run(tools_for(server, design))
    assert "log_agent_activity" in tools and "get_daily_digest" in tools
    with admin(server) as client:
        quick = client.post(
            "/api/admin/keys/quick",
            json={"name": "Marketing Hermes on VPS 2", "project_id": "gamma-shop"},
        )
        assert quick.status_code == 201, quick.text
        body = quick.json()
        assert body["role"] == "marketing" and body["agent_id"] == "marketing-hermes-on-vps-2"
        assert body["plaintext"].startswith("sb_gamma-shop_marketing_marketing-hermes-on-vps-2_")
        assert "claude mcp add" in body["command"] and body["mcp_url"].endswith("/mcp")
    since = asyncio.run(call(server, marketing, "get_company_log_delta", {"since_entry_id": 0}))[
        "latest_id"
    ]
    logged = asyncio.run(
        call(
            server,
            design,
            "log_agent_activity",
            {"summary": "Switched headings to Inter 600", "kind": "task", "affects": ["marketing"]},
        )
    )
    assert logged["status"] == "ok" and logged["affects"] == ["marketing"]
    delta = asyncio.run(call(server, marketing, "get_company_log_delta", {"since_entry_id": since}))
    assert delta["entries"][0]["relevance"] == "affects_you"
    digest = asyncio.run(call(server, marketing, "get_daily_digest", {}))
    assert digest["counts"]["task"] >= 1
    with admin(server) as client:
        content = client.get("/api/admin/projects/gamma-shop/files/AGENT_CHANGELOG.md").json()[
            "content"
        ]
        assert "Switched headings to Inter 600" in content and "### " in content
    for _ in range(10):
        wrong = httpx.get(f"{server}/api/admin/status", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code in (401, 429)
    locked = httpx.get(f"{server}/api/admin/status", headers={"Authorization": "Bearer wrong"})
    assert locked.status_code == 429
