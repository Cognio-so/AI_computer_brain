import pytest

from second_brain.auth import (
    AuthError,
    Principal,
    build_key,
    extract_token,
    generate_secret,
    hash_key,
    key_prefix,
    parse_key,
)
from second_brain.guards import clamp_summary, find_secret


def test_key_round_trip():
    secret = generate_secret()
    assert len(secret) == 43 and secret.isalnum()
    raw = build_key("acme-corp", "sales", "hermes-sales-01", secret)
    parsed = parse_key(raw)
    assert parsed is not None
    assert (parsed.project_id, parsed.role, parsed.agent_id, parsed.secret) == (
        "acme-corp",
        "sales",
        "hermes-sales-01",
        secret,
    )
    assert hash_key(raw) == hash_key(raw)
    assert hash_key(raw) != hash_key(raw[:-1] + ("A" if raw[-1] != "A" else "B"))
    assert key_prefix(parsed).startswith("sb_acme-corp_sales_hermes-sales-01_")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sb_acme_corp_sales_agent_secret",
        "xx_acme-corp_sales_agent-1_" + "a" * 43,
        "sb_ACME_sales_agent-1_" + "a" * 43,
        "sb_acme-corp_sales_agent-1_short",
        "sb_acme-corp_sales_agent-1_" + "a" * 20 + "!",
    ],
)
def test_malformed_keys_are_rejected(raw):
    assert parse_key(raw) is None


def test_extract_token_prefers_bearer():
    token = "sb_acme-corp_sales_agent-1_" + "a" * 43
    assert extract_token({"authorization": f"Bearer {token}"}) == token
    assert extract_token({"x-brain-key": token}) == token
    assert extract_token({}, f"key={token}") == token
    assert extract_token({"authorization": f"Bearer {token}", "x-brain-key": "other"}) == token
    assert extract_token({}) is None


def test_principal_project_resolution():
    agent = Principal("acme-corp", "sales", "hermes-sales-01", "k1")
    assert agent.resolve_project(None) == "acme-corp"
    assert agent.resolve_project("acme-corp") == "acme-corp"
    with pytest.raises(AuthError):
        agent.resolve_project("beta-fintech")
    master = Principal("*", "admin", "admin", None, is_master=True)
    assert master.resolve_project("beta-fintech") == "beta-fintech"
    with pytest.raises(AuthError):
        master.resolve_project(None)
    assert agent.actor("ui-subagent") == "sales/hermes-sales-01[ui-subagent]"


def test_secret_guard_and_summary_clamp():
    assert find_secret("sk-abcdefghijklmnopqrstuvwxyz1234") == "openai-style key"
    assert find_secret("password = SuperSecretValue123456") == "credential assignment"
    assert (
        find_secret("postgresql://user:pa55word@db.example.com/app")
        == "connection string with password"
    )
    assert find_secret("Pro tier is $49 per month with a 20% annual discount") is None
    assert clamp_summary("  many   spaces   here ", 160) == "many spaces here"
    assert len(clamp_summary("x" * 500, 160)) == 160


async def test_keys_are_sealed_and_can_be_revealed_and_resealed(service, project):
    from second_brain.keyvault import KeyVault

    record, plaintext = await service.issue_key(project, "sales", "sealed-agent", None)
    assert record.secret_sealed and plaintext not in record.secret_sealed
    assert service.auth.reveal(record) == plaintext
    assert KeyVault("other").open(record.secret_sealed) is None
    assert KeyVault("").seal("x") is None and KeyVault("").open("y") is None
    assert await service.auth.reseal("bm_new-master-key-0123") >= 1
    fresh = await service.find_key(project, record.key_id)
    assert service.auth.reveal(fresh) == plaintext and fresh.secret_sealed != record.secret_sealed
    info = service.connect_info(project, fresh)
    assert info["plaintext"] == plaintext and plaintext in info["snippets"]["hermes"]


async def test_old_databases_gain_the_sealed_column(tmp_path):
    import sqlite3

    from second_brain.config import Settings
    from second_brain.database import Database

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE api_keys (key_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "department_role TEXT NOT NULL, agent_id TEXT NOT NULL, label TEXT, key_prefix TEXT NOT NULL, "
        "compound_key_hash TEXT UNIQUE NOT NULL, created_at DATETIME, last_used_at DATETIME, "
        "revoked_at DATETIME, last_sync_log_id INTEGER NOT NULL DEFAULT 0, last_sync_at DATETIME)"
    )
    con.commit()
    con.close()
    settings = Settings(_env_file=None, admin_master_key="bm_x", brain_files_dir=tmp_path / "bf")
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}", settings.sql_dir)
    await db.init()
    await db.init()
    await db.close()
    con = sqlite3.connect(path)
    columns = [row[1] for row in con.execute("PRAGMA table_info(api_keys)")]
    con.close()
    assert columns.count("secret_sealed") == 1
