import json
import subprocess
from pathlib import Path

import pytest
from conftest import make_service, make_settings

from second_brain import repo_analysis
from second_brain.config import Settings
from second_brain.documents import DocumentError, extract_text, normalize_access
from second_brain.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    extract_json,
    mask_key,
    validate_provider,
)
from second_brain.repo_analysis import (
    AnalysisRun,
    build_user_prompt,
    clone_url_for,
    collect_digest,
    describe_github,
    parse_repo_url,
)


def test_llm_config_resolution_and_masking():
    settings = Settings(_env_file=None, llm_provider="OpenAI", llm_api_key="sk-abcdefghijklmnop")
    config = LLMConfig.from_settings(settings)
    assert config.enabled and config.resolved_model == "gpt-6.1-sol"
    assert config.resolved_base_url == "https://api.openai.com/v1"
    described = config.describe()
    assert described["key_hint"] == "sk-abc...mnop" and described["key_set"] is True
    assert [p["key"] for p in described["providers"]] == [
        "openai",
        "anthropic",
        "gemini",
        "openrouter",
        "bedrock",
        "custom",
    ]
    custom = LLMConfig("custom", "key", "my-model", "")
    assert custom.enabled is False
    assert LLMConfig(
        "custom", "key", "my-model", "http://localhost:11434/v1/"
    ).resolved_base_url.endswith("/v1")
    assert Settings(_env_file=None, llm_provider="nope").llm_provider == ""
    assert validate_provider("Anthropic") == "anthropic"
    with pytest.raises(LLMError):
        validate_provider("bard")
    assert mask_key("short") == "*****" and mask_key("") == ""
    with pytest.raises(LLMError) as error:
        LLMClient(LLMConfig("openai", "", "", ""))
    assert error.value.status == 409


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"sections": []} thanks') == {"sections": []}
    with pytest.raises(LLMError):
        extract_json("no json here")
    with pytest.raises(LLMError):
        extract_json("[1, 2]")


def test_repo_url_parsing():
    assert parse_repo_url("https://github.com/acme/brain.git") == ("acme", "brain")
    assert parse_repo_url("git@github.com:acme/brain.git") == ("acme", "brain")
    with pytest.raises(LLMError):
        parse_repo_url("not a url")
    assert clone_url_for("github.com/acme/brain") == "https://github.com/acme/brain.git"
    assert clone_url_for("git@github.com:acme/brain.git") == "git@github.com:acme/brain.git"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True
    )


def _commit_all(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def _fake_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "README.md").write_text(
        "# Shop\n\nAn online shop for widgets. Pricing: Pro $49/mo.\n", encoding="utf-8"
    )
    (root / "package.json").write_text(
        json.dumps({"name": "shop", "dependencies": {"next": "15.0.0"}}), encoding="utf-8"
    )
    (root / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: shop\n    ports: ['3000:3000']\n", encoding="utf-8"
    )
    (root / "src" / "schema.prisma").write_text(
        "model User {\n  id Int @id\n  email String\n}\n", encoding="utf-8"
    )
    (root / "src" / "globals.css").write_text(":root { --brand: #ff0000; }\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "DATABASE_URL=\nSTRIPE_SECRET=sk_live_abcdefghijklmnop\n", encoding="utf-8"
    )
    (root / "node_modules" / "x" / "index.js").write_text("ignored", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")


def test_collect_digest_selects_relevant_files_and_redacts_secrets(tmp_path):
    _fake_repo(tmp_path)
    digest = collect_digest(tmp_path)
    names = [name for name, _ in digest.files]
    assert "README.md" in names and "package.json" in names and "src/schema.prisma" in names
    assert "docker-compose.yml" in names and "src/globals.css" in names
    assert not any("node_modules" in name for name in names) and "logo.png" not in names
    env_text = dict(digest.files)[".env.example"]
    assert "sk_live_abcdefghijklmnop" not in env_text and "<redacted>" in env_text
    prompt = build_user_prompt(
        "Shop", "https://x:token@github.com/acme/shop", digest, {"DESIGN_SYSTEM.md": "old"}
    )
    assert "token" not in prompt.split("Repository:")[1].splitlines()[0]
    assert "### README.md" in prompt and "### DESIGN_SYSTEM.md" in prompt


async def test_analyzer_applies_sections_findings_and_schema(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, llm_provider="openai", llm_api_key="sk-test-key-12345678")
    service = await make_service(settings)
    try:
        project = await service.create_project("shop", "Shop")
        repo = tmp_path / "upstream"
        _fake_repo(repo)
        _commit_all(repo)
        await service.update_project(
            "shop",
            settings={
                "github": {"repo_url": "https://github.com/acme/shop", "analyze_on_push": True}
            },
        )
        monkeypatch.setattr(repo_analysis, "clone_url_for", lambda url: str(repo))
        plan = {
            "summary": "An online widget shop built with Next.js.",
            "sections": [
                {
                    "file": "TECH_AND_INFRA.md",
                    "heading": "Stack",
                    "content": "- Next.js 15\n- Prisma\n",
                    "summary": "Stack documented from package.json",
                },
                {
                    "file": "COMPANY_PULSE.md",
                    "heading": "pricing-tiers",
                    "content": "| Plan | Price |\n|---|---|\n| Pro | $49/mo |\n",
                    "summary": "Pro plan is $49 per month",
                },
                {
                    "file": "COMPANY_LOG.md",
                    "heading": "Hot Log",
                    "content": "nope",
                    "summary": "ignored",
                },
                {
                    "file": "DESIGN_SYSTEM.md",
                    "heading": "Brand Colors",
                    "content": "",
                    "summary": "empty",
                },
            ],
            "db_schema": {
                "engine": {
                    "name": "main",
                    "engine": "postgresql",
                    "version": "",
                    "purpose": "app data",
                },
                "tables": [
                    {
                        "name": "users",
                        "columns": [{"name": "id", "type": "int", "nullable": False}],
                        "indexes": [],
                        "notes": "",
                    }
                ],
            },
            "findings": [
                {
                    "summary": "Pricing is hard-coded in README, marketing must align copy",
                    "affects": ["marketing"],
                },
                {"summary": "", "affects": []},
            ],
        }
        calls: list[tuple[str, str]] = []

        async def fake_complete(self, system, user, max_tokens=16000):
            calls.append((system, user))
            return "```json\n" + json.dumps(plan) + "\n```"

        monkeypatch.setattr(LLMClient, "complete", fake_complete)
        assert service.analyzer.auto_trigger(
            await service.get_project("shop"), "https://github.com/acme/shop.git"
        )
        result = await service.analyzer.run("shop", "manual")
        assert result["state"] == "done", result
        applied = result["result"]
        assert applied["sections_updated"] == 2 and applied["sections_skipped"] == 2
        assert applied["findings"] == 1 and applied["tables"] == 1
        assert "README.md" in calls[0][1] and "Allowed files and headings" in calls[0][0]
        tech = await service.vault(project).read("TECH_AND_INFRA.md")
        assert "- Next.js 15" in tech
        pulse = await service.vault(project).read("COMPANY_PULSE.md")
        assert "| Pro | $49/mo |" in pulse
        schema = await service.vault(project).read("DB_SCHEMA_STATE.md")
        assert "### users" in schema
        changelog = await service.vault(project).read("AGENT_CHANGELOG.md")
        assert "Pricing is hard-coded" in changelog
        refreshed = await service.get_project("shop")
        github = describe_github(refreshed, "http://testserver")
        assert (
            github["last_analysis"]["ok"] is True
            and github["last_analysis"]["sections_updated"] == 2
        )
        assert github["token_set"] is False and github["webhook_url"].endswith(
            "/webhooks/shop/github"
        )
        second = await service.analyzer.run("shop", "push")
        assert second["result"]["sections_updated"] == 0
        assert service.analyzer.status("shop")["state"] == "done"
        view = service.project_view(refreshed)
        assert view["github_connected"] is True and "token" not in view["settings"]["github"]
    finally:
        await service.stop()
        await service.db.close()


async def test_analyzer_reports_llm_failures(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, llm_provider="anthropic", llm_api_key="sk-ant-test-key")
    service = await make_service(settings)
    try:
        await service.create_project("shop", "Shop")
        run = await service.analyzer.run("shop", "manual")
        assert run["state"] == "failed" and "connect a GitHub repository" in run["error"]
        await service.update_project(
            "shop", settings={"github": {"repo_url": "https://github.com/acme/shop"}}
        )

        async def boom(self, system, user, max_tokens=16000):
            raise LLMError("Anthropic rejected the API key", 401)

        monkeypatch.setattr(LLMClient, "complete", boom)
        monkeypatch.setattr(repo_analysis, "clone_url_for", lambda url: str(tmp_path / "missing"))
        run = await service.analyzer.run("shop", "manual")
        assert run["state"] == "failed" and run["error"].startswith("git:")
        assert AnalysisRun("shop", "manual").to_dict()["state"] == "running"
    finally:
        await service.stop()
        await service.db.close()


def test_document_text_extraction_and_access_lists():
    text, media = extract_text("notes.md", b"\xef\xbb\xbf# Hi\r\n\r\n\r\n\r\nbody  \r\n")
    assert text == "# Hi\n\nbody\n" and media == "text/markdown"
    html_text, _ = extract_text(
        "page.html",
        b"<html><style>x{}</style><body><h1>Title</h1><p>Para &amp; more</p></body></html>",
    )
    assert "Title" in html_text and "Para & more" in html_text and "x{}" not in html_text
    with pytest.raises(DocumentError) as error:
        extract_text("archive.zip", b"PK")
    assert error.value.status == 415
    with pytest.raises(DocumentError):
        extract_text("empty.txt", b"   ")
    assert normalize_access(None) == ["*"] and normalize_access("all") == ["*"]
    assert normalize_access("hermes-sales-01, role:marketing") == [
        "hermes-sales-01",
        "role:marketing",
    ]
    assert normalize_access('["a", "*"]') == ["*"] and normalize_access([]) == ["*"]


async def test_document_store_and_agent_visibility(tmp_path):
    settings = make_settings(tmp_path)
    service = await make_service(settings)
    try:
        project = await service.create_project("acme-corp", "Acme Corp")
        sales_record, sales_key = await service.issue_key(project, "sales", "hermes-sales-01", None)
        ops_record, ops_key = await service.issue_key(project, "ops", "openclaw-home", None)
        sales = await service.auth.authenticate(sales_key)
        ops = await service.auth.authenticate(ops_key)
        shared = await service.documents.add(
            project, b"# SOP\n\nRefunds take 5 days.\n", "refund-sop.md", "Refund SOP", "*"
        )
        private = await service.documents.add(
            project, b"Pricing matrix: Pro 49, Team 99", "pricing.txt", None, "hermes-sales-01"
        )
        assert "*" in shared.access and shared.to_dict()["shared_with_all"] is True
        assert private.title == "pricing" and private.access == ["hermes-sales-01"]
        assert (service.documents.folder(project) / shared.file_name).is_file()
        assert sorted(d.title for d in await service.documents.visible(project, sales)) == [
            "Refund SOP",
            "pricing",
        ]
        assert [d.title for d in await service.documents.visible(project, ops)] == ["Refund SOP"]
        chunk = await service.documents.read(project, sales, private.doc_id, 0, 200)
        assert chunk["content"].startswith("Pricing matrix") and chunk["next_offset"] is None
        with pytest.raises(DocumentError) as error:
            await service.documents.read(project, ops, private.doc_id, 0, 200)
        assert error.value.status == 403
        hits = await service.documents.search(project, ops, "refunds days", 5)
        assert hits["hits"] and hits["hits"][0]["doc_id"] == shared.doc_id
        assert not (await service.documents.search(project, ops, "pricing", 5))["hits"]
        who = await service.whoami(ops, project)
        assert who["document_count"] == 1 and "read_document" in who["tools"]
        delta = await service.log_delta(sales, project, 0, 50, False)
        assert any(
            e["type"] == "document_added" and e["relevance"] == "affects_you"
            for e in delta["entries"]
        )
        updated = await service.documents.update(
            project, private.doc_id, "Pricing matrix", "role:ops"
        )
        assert updated.access == ["role:ops"]
        assert sorted(d.title for d in await service.documents.visible(project, ops)) == [
            "Pricing matrix",
            "Refund SOP",
        ]
        await service.documents.delete(project, shared.doc_id)
        assert not (service.documents.folder(project) / shared.file_name).exists()
        assert len(await service.documents.list(project)) == 1
        quick = await service.quick_project("Acme Corp")
        assert (
            quick["project"]["project_id"] == "acme-corp-02"
            and quick["agent_id"] == "primary-agent"
        )
        assert quick["role"] == "admin" and quick["plaintext"].startswith(
            "sb_acme-corp-02_admin_primary-agent_"
        )
    finally:
        await service.stop()
        await service.db.close()


async def test_effort_setting_and_reasoning_retry(monkeypatch):
    anthropic = LLMConfig("anthropic", "k", "", "", effort="xhigh")
    assert anthropic.resolved_effort == "xhigh" and anthropic.provider_effort() == "xhigh"
    openai = LLMConfig("openai", "k", "", "", effort="max")
    assert openai.resolved_effort == "" and openai.provider_effort() == ""
    openai_high = LLMConfig("openai", "k", "", "", effort="HIGH")
    assert openai_high.provider_effort() == "high"
    assert openai_high.describe()["effort"] == "high"
    assert LLMConfig("openai", "k", "", "").describe()["effort"] == "default"
    assert (
        Settings(
            _env_file=None, llm_provider="openai", llm_api_key="k", llm_effort="low"
        ).llm_effort
        == "low"
    )
    bodies: list[dict] = []

    def handler(request):
        import httpx as _httpx

        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return _httpx.Response(
                400,
                json={
                    "error": {"message": "Unrecognized request argument supplied: reasoning_effort"}
                },
            )
        return _httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        )

    import httpx as real_httpx

    original_client = real_httpx.AsyncClient
    monkeypatch.setattr(
        "second_brain.llm.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=real_httpx.MockTransport(handler)),
    )
    client = LLMClient(openai_high)
    assert await client.complete("sys", "hi", 32) == "OK"
    assert bodies[0]["reasoning_effort"] == "high" and "reasoning_effort" not in bodies[1]


async def test_document_metadata_and_sources(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path, llm_provider="openai", llm_api_key="sk-test-key-12345678", git_push_enabled=False
    )
    service = await make_service(settings)
    try:
        project = await service.create_project("acme-corp", "Acme Corp")

        async def fake_complete(self, system, user, max_tokens=16000):
            assert "Refund policy" in user
            return json.dumps(
                {
                    "summary": "Refund rules for customers: windows, exceptions and who approves.",
                    "topics": ["refunds", "returns", "approval"],
                    "departments": ["sales", "ops"],
                }
            )

        monkeypatch.setattr(LLMClient, "complete", fake_complete)
        document = await service.documents.add(
            project, b"Refunds are accepted within 14 days.", "policy.txt", "Refund policy", "*"
        )
        meta = await service.documents.describe(project, document.doc_id)
        assert meta["topics"] == ["refunds", "returns", "approval"]
        refreshed = await service.documents.get(project, document.doc_id)
        assert refreshed.summary_text.startswith("Refund rules") and refreshed.departments == [
            "sales",
            "ops",
        ]
        listed = refreshed.to_dict()
        assert (
            listed["topics"] == ["refunds", "returns", "approval"]
            and "Refund rules" in listed["summary"]
        )
        _, key = await service.issue_key(project, "ops", "openclaw-home", None)
        ops = await service.auth.authenticate(key)
        hits = await service.documents.search(project, ops, "returns", 5)
        assert hits["hits"][0]["doc_id"] == document.doc_id and hits["hits"][0]["offset"] == 0
        who = await service.whoami(ops, project)
        assert who["documents"][0]["topics"] == ["refunds", "returns", "approval"]
        assert who["sources"]["documents_dir"] == "docs/" and who["sources"]["backup_repo"] is None
        started: list[str] = []
        monkeypatch.setattr(
            service.analyzer, "start", lambda pid, trigger="manual": started.append(trigger) or True
        )
        await service.connect_github(project, "https://github.com/acme/app")
        assert started == ["connect"]
    finally:
        await service.stop()
        await service.db.close()
