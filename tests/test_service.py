import asyncio

import pytest
from conftest import issue, make_service, make_settings

from second_brain.auth import AuthError, Principal
from second_brain.config import CANONICAL_FILES
from second_brain.git_sync import GitRepo
from second_brain.markdown_engine import parse
from second_brain.service import BrainError


async def test_create_project_provisions_vault_and_git(service, project):
    vault = service.vault(project)
    for name in CANONICAL_FILES:
        assert vault.path(name).is_file()
    repo = service.git_repo(project.project_id, vault.root)
    assert await repo.is_repo()
    assert await repo.head()
    assert not (vault.root / ".git").exists()
    assert (service.settings.brain_files_dir / ".git-store" / "acme-corp" / "HEAD").is_file()
    listed = await service.list_projects()
    assert [p.project_id for p in listed] == ["acme-corp"]
    recent = await service.recent_log(project, 10)
    assert recent and recent[0]["type"] == "system"
    with pytest.raises(BrainError) as error:
        await service.create_project("acme-corp", "dup")
    assert error.value.code == "CONFLICT"
    with pytest.raises(BrainError):
        await service.create_project("Bad_Id", "bad")


async def test_key_lifecycle_and_whoami(service, project):
    principal, plaintext, record = await issue(service, project, "sales", "hermes-sales-01")
    assert plaintext.startswith("sb_acme-corp_sales_hermes-sales-01_")
    assert principal.project_id == "acme-corp" and principal.role == "sales"
    me = await service.whoami(principal, project)
    assert me["permissions"]["COMPANY_PULSE.md"] == "rw"
    assert me["permissions"]["TECH_AND_INFRA.md"] == "r"
    assert "sync_db_schema" not in me["tools"]
    assert any("pricing-tiers" in g["update"] for g in me["file_guide"])
    await service.revoke_key(project, record.key_id)
    with pytest.raises(AuthError):
        await service.auth.authenticate(plaintext)
    with pytest.raises(BrainError):
        await service.issue_key(project, "finance", "agent-x", None)


async def test_update_read_delta_and_commit(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    marketing, _, _ = await issue(service, project, "marketing", "hermes-marketing-01")
    before = await service.read_section(sales, project, "COMPANY_PULSE.md", "Pricing Tiers", None)
    result = await service.update_section(
        sales,
        project,
        "COMPANY_PULSE.md",
        "Pricing Tiers",
        "| Plan | Price |\n|---|---|\n| Pro | $59 / month |",
        "Pro tier raised to $59/mo",
        expected_etag=before["etag"],
    )
    assert result.status == "ok" and result.new_etag != before["etag"]
    again = await service.read_section(
        marketing, project, "COMPANY_PULSE.md", "pricing-tiers", before["etag"]
    )
    assert again["status"] == "ok" and "$59" in again["content"]
    cached = await service.read_section(
        marketing, project, "COMPANY_PULSE.md", "pricing-tiers", again["etag"]
    )
    assert cached["status"] == "NOT_MODIFIED"
    delta = await service.log_delta(marketing, project, 0, 50, False)
    summaries = [e["summary"] for e in delta["entries"]]
    assert "Pro tier raised to $59/mo" in summaries
    assert delta["latest_id"] >= result.log_id
    hot_log = await service.vault(project).read("COMPANY_LOG.md")
    assert "Pro tier raised to $59/mo" in hot_log
    await service.git.drain()
    entry = await service.company_log(project).get(result.log_id)
    assert entry.commit_sha
    diff = await service.diff(project, result.log_id)
    assert "$59" in diff["diff"]


async def test_rbac_enforced(service, project):
    engineering, _, _ = await issue(service, project, "engineering", "claude-code-01")
    marketing, _, _ = await issue(service, project, "marketing", "hermes-marketing-01")
    with pytest.raises(BrainError) as error:
        await service.update_section(
            engineering,
            project,
            "COMPANY_PULSE.md",
            "Pricing Tiers",
            "x",
            "engineering tries pricing",
        )
    assert error.value.code == "FORBIDDEN"
    with pytest.raises(BrainError) as error:
        await service.update_section(
            marketing, project, "COMPANY_PULSE.md", "Pricing Tiers", "x", "marketing tries pricing"
        )
    assert error.value.code == "FORBIDDEN"
    ok = await service.update_section(
        marketing,
        project,
        "COMPANY_PULSE.md",
        "Shipped Recently",
        "- New landing page",
        "Shipped landing page",
        mode="append",
    )
    assert ok.status == "ok"
    with pytest.raises(BrainError) as error:
        await service.update_section(
            engineering, project, "COMPANY_LOG.md", "Hot Log", "x", "tamper"
        )
    assert error.value.code == "FORBIDDEN"


async def test_conflict_detection(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    current = await service.read_section(sales, project, "COMPANY_PULSE.md", "Open Decisions", None)
    await service.update_section(
        sales,
        project,
        "COMPANY_PULSE.md",
        "Open Decisions",
        "- Decision A",
        "Decision A",
        expected_etag=current["etag"],
    )
    with pytest.raises(BrainError) as error:
        await service.update_section(
            sales,
            project,
            "COMPANY_PULSE.md",
            "Open Decisions",
            "- Decision B",
            "Decision B",
            expected_etag=current["etag"],
        )
    assert error.value.code == "CONFLICT"
    assert "Decision A" in error.value.extra["current_content"]


async def test_parallel_appends_never_lose_lines(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    await asyncio.gather(
        *[
            service.update_section(
                sales,
                project,
                "COMPANY_PULSE.md",
                "Open Decisions",
                f"- parallel {i}",
                f"parallel {i}",
                mode="append",
                subagent_role=f"sub-{i}",
            )
            for i in range(12)
        ]
    )
    body = (await service.read_section(sales, project, "COMPANY_PULSE.md", "Open Decisions", None))[
        "content"
    ]
    for i in range(12):
        assert f"- parallel {i}" in body


async def test_optimistic_concurrency_with_subagents(service, project):
    design, _, _ = await issue(service, project, "design", "hermes-ui-01")
    current = await service.read_section(design, project, "DESIGN_SYSTEM.md", "Brand Colors", None)
    results = await asyncio.gather(
        *[
            service.update_section(
                design,
                project,
                "DESIGN_SYSTEM.md",
                "Brand Colors",
                f"| token | #{i:06d} |",
                f"colour {i}",
                expected_etag=current["etag"],
                subagent_role=f"sub-{i}",
            )
            for i in range(6)
        ],
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, BrainError) and r.code == "CONFLICT"]
    assert len(successes) == 1 and len(conflicts) == 5


async def test_secret_guard_and_validation(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    with pytest.raises(BrainError) as error:
        await service.update_section(
            sales,
            project,
            "COMPANY_PULSE.md",
            "Open Decisions",
            "key sk-abcdefghijklmnopqrstuvwxyz123456",
            "leak",
        )
    assert error.value.code == "SECRET_DETECTED"
    with pytest.raises(BrainError) as error:
        await service.update_section(
            sales, project, "COMPANY_PULSE.md", "Open Decisions", "fine", ""
        )
    assert error.value.code == "INVALID"
    with pytest.raises(BrainError) as error:
        await service.update_section(
            sales, project, "NOT_A_FILE.md", "Open Decisions", "fine", "summary here"
        )
    assert error.value.code == "INVALID"
    with pytest.raises(BrainError) as error:
        await service.update_section(
            sales, project, "COMPANY_PULSE.md", "Missing Heading", "fine", "summary here"
        )
    assert error.value.code == "NOT_FOUND"
    created = await service.update_section(
        sales,
        project,
        "COMPANY_PULSE.md",
        "Partnerships",
        "- Acme x Beta",
        "New partnerships section",
        mode="create",
    )
    assert created.status == "ok" and created.slug == "partnerships"


async def test_approval_flow(service, project, master):
    permissions = await service.vault(project).read("ACCESS_PERMISSIONS.md")
    updated = permissions.replace(
        "| COMPANY_PULSE.md | rw | r | rw | rw | rw | r |",
        "| COMPANY_PULSE.md | rw | r | rw-approve | rw | rw | r |",
    )
    assert updated != permissions
    await service.update_file(
        master, project, "ACCESS_PERMISSIONS.md", updated, "Pricing changes by sales need approval"
    )
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    me = await service.whoami(sales, project)
    assert me["permissions"]["COMPANY_PULSE.md"] == "rw-approve" and "get_proposal" in me["tools"]
    pending = await service.update_section(
        sales, project, "COMPANY_PULSE.md", "Pricing Tiers", "| Pro | $99 |", "Pro tier to $99"
    )
    assert pending.status == "PENDING_APPROVAL" and pending.proposal_id
    assert (
        "$99"
        not in (
            await service.read_section(sales, project, "COMPANY_PULSE.md", "Pricing Tiers", None)
        )["content"]
    )
    proposals = await service.list_proposals(project, "pending")
    assert [p.proposal_id for p in proposals] == [pending.proposal_id]
    decided = await service.decide_proposal(project, pending.proposal_id, True, "looks right")
    assert decided.status == "approved"
    assert (
        "$99"
        in (await service.read_section(sales, project, "COMPANY_PULSE.md", "Pricing Tiers", None))[
            "content"
        ]
    )
    fetched = await service.get_proposal(sales, project, pending.proposal_id)
    assert fetched.status == "approved" and fetched.reviewer_note == "looks right"
    rejected = await service.update_section(
        sales, project, "COMPANY_PULSE.md", "Pricing Tiers", "| Pro | $1 |", "Pro tier to $1"
    )
    outcome = await service.decide_proposal(project, rejected.proposal_id, False, "no")
    assert outcome.status == "rejected"


async def test_sync_banner_and_inline_inbox(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    marketing, _, _ = await issue(service, project, "marketing", "hermes-marketing-01")
    await service.log_delta(marketing, project, 0, 100, True)
    assert await service.sync_banner(marketing, project) is None
    await service.update_section(
        sales, project, "COMPANY_PULSE.md", "Pricing Tiers", "| Pro | $79 |", "Pro tier to $79"
    )
    banner = await service.sync_banner(marketing, project)
    assert banner is not None and banner["unread"] == 1
    assert banner["inbox"][0]["summary"] == "Pro tier to $79"
    assert await service.sync_banner(marketing, project) is None
    for i in range(5):
        await service.update_section(
            sales,
            project,
            "COMPANY_PULSE.md",
            "Open Decisions",
            f"- d{i}",
            f"decision {i}",
            mode="append",
        )
    banner = await service.sync_banner(marketing, project)
    assert (
        banner is not None and "inbox" not in banner and "get_company_log_delta" in banner["hint"]
    )


async def test_relevance_filter_hides_unwatched_changes(service, project):
    engineering, _, _ = await issue(service, project, "engineering", "claude-code-01")
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    start = (await service.log_delta(sales, project, 0, 100, True))["latest_id"]
    await service.update_section(
        engineering,
        project,
        "TECH_AND_INFRA.md",
        "Stack",
        "| Python | 3.13 |",
        "Python upgraded to 3.13",
    )
    delta = await service.log_delta(sales, project, start, 100, False)
    assert delta["entries"] == [] and delta["latest_id"] > start
    delta_all = await service.log_delta(engineering, project, start, 100, False)
    assert [e["summary"] for e in delta_all["entries"]] == ["Python upgraded to 3.13"]


async def test_sync_db_schema(service, project):
    engineering, _, _ = await issue(service, project, "engineering", "claude-code-01")
    payload = {
        "engine": {"name": "main", "engine": "PostgreSQL", "version": "16", "purpose": "app"},
        "tables": [
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False},
                    {"name": "total", "type": "numeric(12,2)", "nullable": False},
                ],
                "indexes": ["orders_pkey"],
            },
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "type": "bigint", "nullable": False},
                    {"name": "email", "type": "text", "nullable": False},
                ],
            },
        ],
    }
    first = await service.sync_db_schema(engineering, project, payload, "Initial schema import")
    assert first["status"] == "ok" and sorted(first["changed_tables"]) == ["customers", "orders"]
    doc = parse(await service.vault(project).read("DB_SCHEMA_STATE.md"))
    assert doc.find("orders") is not None and doc.find("orders").level == 3
    assert "Initial schema import" in doc.body(doc.require("Migration Log"))
    second = await service.sync_db_schema(engineering, project, payload, "No changes")
    assert second["status"] == "NOT_MODIFIED"
    payload["tables"][0]["columns"].append(
        {"name": "refund_reason", "type": "text", "nullable": True}
    )
    third = await service.sync_db_schema(engineering, project, payload, "Added refund_reason")
    assert third["changed_tables"] == ["orders"]


async def test_hot_log_rotation_archives(tmp_path):
    settings = make_settings(tmp_path, hot_log_size=5)
    service = await make_service(settings)
    try:
        project = await service.create_project("beta", "Beta")
        sales, _, _ = await issue(service, project, "sales", "agent-1")
        for i in range(8):
            await service.update_section(
                sales,
                project,
                "COMPANY_PULSE.md",
                "Open Decisions",
                f"- item {i}",
                f"item {i}",
                mode="append",
            )
        hot = await service.vault(project).read("COMPANY_LOG.md")
        assert hot.count("\n- #") == 5
        archives = service.vault(project).list_archives()
        assert len(archives) == 1
        archived = await service.vault(project).read(archives[0], allow_archive=True)
        assert "item 0" in archived and "item 7" not in archived
    finally:
        await service.stop()
        await service.db.close()


async def test_update_file_and_isolation(service, project, master):
    other = await service.create_project("beta-fintech", "Beta")
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    with pytest.raises(BrainError) as error:
        await service.resolve(sales, "beta-fintech")
    assert error.value.code == "FORBIDDEN"
    text = await service.vault(other).read("COMPANY_PULSE.md")
    result = await service.update_file(
        master,
        other,
        "COMPANY_PULSE.md",
        text.replace("- (no positioning documented yet)", "- Mission: make agents agree"),
        "Mission written",
        expected_etag=parse(text).etag,
    )
    assert result.status == "ok" and "mission-positioning" in result.extra["changed_sections"]
    with pytest.raises(BrainError) as error:
        await service.update_file(
            master, other, "COMPANY_PULSE.md", text, "stale write", expected_etag=parse(text).etag
        )
    assert error.value.code == "CONFLICT"


async def test_agent_changelog_affects_and_digest(service, project):
    design, _, _ = await issue(service, project, "design", "hermes-ui-01")
    marketing, _, _ = await issue(service, project, "marketing", "hermes-marketing-01")
    start = (await service.log_delta(marketing, project, 0, 100, True))["latest_id"]
    result = await service.log_activity(
        design,
        project,
        "Primary button radius changed to 12px in all templates",
        "task",
        ["marketing", "engineering"],
        "Slides and web components must pick up the new radius",
    )
    assert result["status"] == "ok" and result["affects"] == ["marketing", "engineering"]
    doc = parse(await service.vault(project).read("AGENT_CHANGELOG.md"))
    body = doc.body(doc.require("design"))
    assert "hermes-ui-01" in body and "[task] Primary button radius" in body
    assert "affects: marketing, engineering" in body and "(no entries yet)" not in body
    assert "Primary button radius" in doc.body(doc.require("Daily Digest"))
    delta = await service.log_delta(marketing, project, start, 100, False)
    entry = next(e for e in delta["entries"] if "button radius" in e["summary"])
    assert entry["relevance"] == "affects_you" and "adjust" in entry["hint"]
    with pytest.raises(BrainError) as error:
        await service.update_section(
            design, project, "AGENT_CHANGELOG.md", "marketing", "- forged", "forged entry"
        )
    assert error.value.code == "FORBIDDEN"
    with pytest.raises(BrainError) as error:
        await service.log_activity(design, project, "bad department", "task", ["finance"], None)
    assert error.value.code == "INVALID"
    digest = await service.daily_digest(marketing, project, None)
    assert digest["counts"]["task"] == 1
    assert any("button radius" in e["summary"] for e in digest["entries"])
    me = await service.whoami(design, project)
    assert me["today"]["my_entries"] == 1 and me["today"]["findings_logged"] is False
    await service.log_activity(
        design,
        project,
        "Old PDF templates still use the 8px radius",
        "finding",
        ["marketing"],
        None,
    )
    me = await service.whoami(design, project)
    assert me["today"]["findings_logged"] is True


async def test_section_update_auto_affects_watching_departments(service, project):
    sales, _, _ = await issue(service, project, "sales", "hermes-sales-01")
    marketing, _, _ = await issue(service, project, "marketing", "hermes-marketing-01")
    engineering, _, _ = await issue(service, project, "engineering", "claude-code-01")
    start = (await service.log_delta(marketing, project, 0, 100, True))["latest_id"]
    await service.update_section(
        sales, project, "COMPANY_PULSE.md", "Pricing Tiers", "| Pro | $69 |", "Pro tier to $69"
    )
    entry = (await service.log_delta(marketing, project, start, 100, False))["entries"][0]
    assert "marketing" in entry["affects"] and entry["relevance"] == "affects_you"
    assert (await service.log_delta(engineering, project, start, 100, False))["entries"] == []


async def test_changelog_dedupe_findings_and_agent_events(tmp_path):
    settings = make_settings(tmp_path, changelog_entries_per_department=3)
    service = await make_service(settings)
    try:
        project = await service.create_project("delta-co", "Delta")
        sales, _, record = await issue(service, project, "sales", "hermes-sales-01")
        changelog = await service.vault(project).read("AGENT_CHANGELOG.md")
        assert "New agent hermes-sales-01 joined as sales" in changelog
        first = await service.log_activity(
            sales,
            project,
            "Enterprise prospects ask for SSO before signing",
            "finding",
            ["engineering"],
            None,
        )
        assert first["status"] == "ok"
        again = await service.log_activity(
            sales,
            project,
            "Enterprise prospects ask for SSO  before signing!",
            "finding",
            ["engineering"],
            None,
        )
        assert again["status"] == "DUPLICATE" and again["existing"]["id"] == first["log_id"]
        company_log = parse(await service.vault(project).read("COMPANY_LOG.md"))
        findings = company_log.body(company_log.require("Findings"))
        assert "Enterprise prospects ask for SSO" in findings and "affects: engineering" in findings
        for i in range(4):
            await service.log_activity(
                sales, project, f"Closed deal number {i}", "task", None, None
            )
        doc = parse(await service.vault(project).read("AGENT_CHANGELOG.md"))
        body = doc.body(doc.require("sales"))
        assert body.count("\n- ") + 1 == 3
        assert "Closed deal number 3" in body and "New agent hermes-sales-01" not in body
        archives = service.vault(project).list_archives()
        assert any(name.startswith("logs/changelog-") for name in archives)
        archived = await service.vault(project).read(
            next(name for name in archives if name.startswith("logs/changelog-")),
            allow_archive=True,
        )
        assert "New agent hermes-sales-01 joined as sales" in archived
        await service.revoke_key(project, record.key_id)
        digest = await service.daily_digest(
            Principal("*", "admin", "admin", None, is_master=True), project, None
        )
        assert digest["counts"]["agent"] == 2 and digest["counts"]["finding"] == 1
        assert digest["counts"]["task"] == 4
    finally:
        await service.stop()
        await service.db.close()


async def test_quick_key_derives_project_role_and_agent(tmp_path):
    settings = make_settings(tmp_path)
    service = await make_service(settings)
    try:
        first = await service.quick_key("Sales Hermes on VPS 1", None, "Acme Corp", None)
        assert first["project"]["project_id"] == "acme-corp"
        assert first["role"] == "sales" and first["agent_id"] == "sales-hermes-on-vps-1"
        assert first["plaintext"].startswith("sb_acme-corp_sales_sales-hermes-on-vps-1_")
        assert "claude mcp add" in first["command"] and first["mcp_url"].endswith("/mcp")
        second = await service.quick_key("Claude Code laptop")
        assert second["project"]["project_id"] == "acme-corp" and second["role"] == "engineering"
        third = await service.quick_key("Claude Code laptop")
        assert third["agent_id"] == "claude-code-laptop-02"
        short = await service.quick_key("Bo")
        assert short["role"] == "admin" and short["agent_id"] == "bo-agent"
        forced = await service.quick_key("Helper", role="design")
        assert forced["role"] == "design"
        with pytest.raises(BrainError) as error:
            await service.quick_key("Finance bot", role="finance")
        assert error.value.code == "INVALID"
        await service.create_project("beta", "Beta")
        with pytest.raises(BrainError) as error:
            await service.quick_key("Anyone")
        assert error.value.code == "INVALID" and "beta" in error.value.extra["projects"]
    finally:
        await service.stop()
        await service.db.close()


async def test_legacy_vault_git_folders_are_moved_out_of_the_vault(service, project):
    import shutil

    vault = service.vault(project)
    repo = service.git_repo(project.project_id, vault.root)
    head = await repo.head()
    shutil.move(str(repo.git_dir), str(vault.root / ".git"))
    assert (vault.root / ".git").is_dir() and not await repo.is_repo()
    legacy = GitRepo(vault.root, "t", "t@example.com")
    assert await legacy.is_repo() and await legacy.head() == head
    await service.adopt_vault_histories()
    assert not (vault.root / ".git").exists() and await repo.is_repo()
    assert await repo.head() == head
    await repo.commit_all("[brain] after adoption")
    assert not (vault.root / ".git").exists()
