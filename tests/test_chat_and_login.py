import json

import pytest
from conftest import make_service, make_settings

from second_brain import anthropic_login
from second_brain.chat import clean_history, parse_changes
from second_brain.config import Settings
from second_brain.llm import LLMClient, LLMConfig, LLMError


def test_login_mode_needs_no_key_for_anthropic_only():
    login = LLMConfig("anthropic", "", "", "", "login")
    assert login.enabled and login.uses_login and login.describe()["auth"] == "login"
    assert LLMConfig("openai", "", "", "", "login").enabled is False
    assert Settings(_env_file=None, llm_provider="anthropic", llm_auth="LOGIN").llm_enabled
    assert Settings(_env_file=None, llm_provider="anthropic", llm_auth="nope").llm_auth == "api_key"
    assert Settings(_env_file=None, llm_provider="openai", llm_auth="login").llm_enabled is False
    assert isinstance(anthropic_login.install_instructions(), list)


def test_history_and_change_parsing():
    with pytest.raises(LLMError):
        clean_history([{"role": "assistant", "content": "hi"}])
    history = clean_history(
        [{"role": "system", "content": "x"}, {"role": "user", "content": "  what is pricing? "}]
    )
    assert history == [{"role": "user", "content": "what is pricing?"}]
    changes = parse_changes(
        [
            {"file": "COMPANY_PULSE.md", "heading": "Pricing Tiers", "content": "| Pro | 49 |"},
            {"file": "COMPANY_LOG.md", "heading": "Hot Log", "content": "nope"},
            {"file": "DESIGN_SYSTEM.md", "heading": "", "content": "x"},
            {"file": "TECH_AND_INFRA.md", "heading": "Stack", "content": "- Go", "mode": "weird"},
            "junk",
        ]
    )
    assert [c["file"] for c in changes] == ["COMPANY_PULSE.md", "TECH_AND_INFRA.md"]
    assert changes[0]["slug"] == "pricing-tiers" and changes[0]["mode"] == "replace"
    assert changes[1]["mode"] == "replace" and changes[1]["summary"]


async def test_assistant_answers_and_applies_changes(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, llm_provider="anthropic", llm_auth="login")
    service = await make_service(settings)
    try:
        project = await service.create_project("acme-corp", "Acme Corp")
        await service.documents.add(
            project, b"Refund policy: refunds within 14 days.", "policy.txt", "Refund policy", "*"
        )
        seen: list[str] = []

        async def fake_complete(self, system, user, max_tokens=16000):
            seen.append(user)
            return json.dumps(
                {
                    "reply": "Refunds are allowed within **14 days** (document 'Refund policy').",
                    "changes": [
                        {
                            "file": "COMPANY_PULSE.md",
                            "heading": "Pricing Tiers",
                            "mode": "replace",
                            "content": "| Plan | Price |\n|---|---|\n| Team | $99/mo |\n",
                            "summary": "Added the Team plan at $99 per month",
                        },
                        {
                            "file": "COMPANY_PULSE.md",
                            "heading": "Refund Policy",
                            "mode": "create",
                            "content": "Refunds within 14 days.\n",
                            "summary": "Documented the refund window",
                        },
                    ],
                }
            )

        monkeypatch.setattr(LLMClient, "complete", fake_complete)
        result = await service.assistant.respond(
            project,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "What is the refund policy? Add a Team plan at 99."},
            ],
        )
        assert "14 days" in result["reply"] and len(result["changes"]) == 2
        assert any(s["type"] == "document" for s in result["sources"])
        asked = next(u for u in seen if "Admin asks" in u)
        assert "Refund policy" in asked and "COMPANY_PULSE.md" in asked
        assert "Conversation so far" in asked
        outcome = await service.assistant.apply(project, result["changes"])
        assert [o["ok"] for o in outcome] == [True, True]
        pulse = await service.vault(project).read("COMPANY_PULSE.md")
        assert "| Team | $99/mo |" in pulse and "## Refund Policy" in pulse
        recent = await service.recent_log(project, 5)
        assert any(e["actor"] == "admin/assistant" for e in recent)
        bad = await service.assistant.apply(
            project, [{"file": "COMPANY_LOG.md", "heading": "Hot Log", "content": "x"}]
        )
        assert bad == []
    finally:
        await service.stop()
        await service.db.close()


async def test_login_flow_requires_the_cli(monkeypatch):
    monkeypatch.setattr(anthropic_login, "ant_binary", lambda: None)
    flow = anthropic_login.LoginFlow()
    with pytest.raises(LLMError) as error:
        await flow.start()
    assert error.value.status == 409 and "ant" in error.value.detail
    with pytest.raises(LLMError):
        await flow.submit_code("abc")
    assert flow.status()["running"] is False
    assert await anthropic_login.profile_status() is None
