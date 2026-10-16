from pathlib import Path

from second_brain.config import REPO_ROOT
from second_brain.rbac import Permission, Target, fail_closed_policy, parse_policy

TEMPLATE = Path(REPO_ROOT, "templates", "default_vault", "ACCESS_PERMISSIONS.md").read_text(
    encoding="utf-8"
)


def test_template_policy_parses():
    policy = parse_policy(TEMPLATE)
    assert policy.roles == ("admin", "engineering", "sales", "marketing", "ops", "design")
    assert policy.permission("sales", "COMPANY_PULSE.md") is Permission.WRITE
    assert policy.permission("sales", "TECH_AND_INFRA.md") is Permission.READ
    assert policy.permission("engineering", "DB_SCHEMA_STATE.md") is Permission.WRITE
    assert policy.permission("design", "DESIGN_SYSTEM.md") is Permission.WRITE
    assert len(policy.agent_rules) >= 5
    assert any("pricing-tiers" in target for _, target in policy.file_guide)


def test_heading_override_wins():
    policy = parse_policy(TEMPLATE)
    assert policy.permission("marketing", "COMPANY_PULSE.md") is Permission.WRITE
    assert policy.permission("marketing", "COMPANY_PULSE.md", "pricing-tiers") is Permission.READ
    assert (
        policy.write_permission("marketing", "COMPANY_PULSE.md", "pricing-tiers") is Permission.NONE
    )
    assert policy.heading_summary("marketing") == {"COMPANY_PULSE.md#pricing-tiers": "r"}


def test_system_files_and_admin_rules():
    policy = parse_policy(TEMPLATE)
    for role in policy.roles:
        assert policy.permission(role, "COMPANY_LOG.md") is Permission.READ
    assert policy.permission("admin", "ACCESS_PERMISSIONS.md") is Permission.WRITE
    assert policy.permission("engineering", "ACCESS_PERMISSIONS.md") is Permission.READ
    assert policy.permission("admin", "COMPANY_PULSE.md", "pricing-tiers") is Permission.WRITE


def test_unknown_role_is_read_only():
    policy = parse_policy(TEMPLATE)
    assert policy.permission("finance", "COMPANY_PULSE.md") is Permission.READ
    assert policy.writable_files("finance") == []


def test_sync_policy_watch_lists():
    policy = parse_policy(TEMPLATE)
    assert policy.watches("sales", "COMPANY_PULSE.md", "pricing-tiers")
    assert policy.watches("sales", "DESIGN_SYSTEM.md", "tone-of-voice")
    assert not policy.watches("sales", "DESIGN_SYSTEM.md", "brand-colors")
    assert not policy.watches("sales", "DB_SCHEMA_STATE.md", "orders")
    assert policy.watches("admin", "DB_SCHEMA_STATE.md", "orders")
    assert policy.cadence["sales"] == "every-turn"


def test_rw_approve_is_detected():
    text = TEMPLATE.replace(
        "| COMPANY_PULSE.md | rw | r | rw | rw | rw | r |",
        "| COMPANY_PULSE.md | rw | r | rw-approve | rw | rw | r |",
    )
    policy = parse_policy(text)
    assert policy.permission("sales", "COMPANY_PULSE.md") is Permission.WRITE_APPROVE
    assert policy.write_permission("sales", "COMPANY_PULSE.md").writable
    assert policy.has_approvals()
    assert not parse_policy(TEMPLATE).has_approvals()


def test_fail_closed_policy():
    policy = fail_closed_policy()
    assert policy.permission("sales", "COMPANY_PULSE.md") is Permission.READ
    assert policy.permission("admin", "COMPANY_PULSE.md") is Permission.WRITE
    assert policy.permission("sales", "COMPANY_PULSE.md", "pricing-tiers") is Permission.READ


def test_target_parsing():
    target = Target.parse("COMPANY_PULSE.md#Pricing Tiers")
    assert target.file_name == "COMPANY_PULSE.md" and target.slug == "pricing-tiers"
    assert target.matches("COMPANY_PULSE.md", "pricing-tiers")
    assert not target.matches("COMPANY_PULSE.md", "open-decisions")
    assert Target.parse("*").matches("ANY.md", None)
