import base64
import json
import time

import httpx
import pytest

from second_brain import oauth
from second_brain.config import Settings
from second_brain.llm import LLMConfig, LLMError, list_models
from second_brain.oauth import OAUTH_PROVIDERS, OAuthFlow, TokenStore, describe_tokens


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _mock(handler):
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


def test_code_extraction_and_jwt():
    assert oauth._extract_code("abc#state1") == ("abc", "state1")
    assert oauth._extract_code("http://localhost:1455/auth/callback?code=xyz&state=s2") == (
        "xyz",
        "s2",
    )
    assert oauth._extract_code("plain") == ("plain", "")
    assert oauth._jwt_payload(_jwt({"email": "me@x.io"}))["email"] == "me@x.io"
    assert oauth._jwt_payload("garbage") == {}
    assert describe_tokens(None) == {"signed_in": False}


async def test_flow_builds_pkce_urls_and_completes(tmp_path, monkeypatch):
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "console.anthropic.com":
            posted.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "account": {"email_address": "me@acme.io"},
                },
            )
        if request.url.host == "auth.openai.com":
            form = dict(httpx.QueryParams(request.content.decode()))
            posted.append(form)
            if form.get("grant_type", "").endswith("token-exchange"):
                return httpx.Response(200, json={"access_token": "sk-derived"})
            return httpx.Response(
                200,
                json={
                    "access_token": "at-2",
                    "refresh_token": "rt-2",
                    "expires_in": 60,
                    "id_token": _jwt(
                        {
                            "email": "dev@acme.io",
                            "https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"},
                        }
                    ),
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(oauth, "_http", _mock(handler))
    store = TokenStore(tmp_path / "oauth.json")
    flow = OAuthFlow(store)
    started = await flow.start("anthropic")
    assert started["url"].startswith("https://claude.ai/oauth/authorize?")
    assert "code_challenge=" in started["url"] and "state=" in started["url"]
    assert started["listening"] is False
    with pytest.raises(LLMError):
        await flow.complete("")
    result = await flow.complete(f"the-code#{flow.state}")
    assert result["signed_in"] and result["account"] == "me@acme.io"
    assert posted[-1]["code"] == "the-code" and posted[-1]["code_verifier"]
    stored = store.get("anthropic")
    assert stored["access_token"] == "at-1" and stored["refresh_token"] == "rt-1"

    started = await flow.start("openai", try_local_server=False)
    assert "auth.openai.com" in started["url"] and started["listening"] is False
    result = await flow.complete(f"http://localhost:1455/auth/callback?code=c2&state={flow.state}")
    assert result["api_key_derived"] is True and result["account"] == "dev@acme.io"
    entry = store.get("openai")
    assert entry["api_key"] == "sk-derived" and entry["account_id"] == "acct_1"
    with pytest.raises(LLMError):
        await flow.start("custom")
    with pytest.raises(LLMError):
        await OAuthFlow(store).complete("x")


async def test_refresh_and_config_resolution(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-2"
        return httpx.Response(200, json={"access_token": "at-3", "expires_in": 3600})

    monkeypatch.setattr(oauth, "_http", _mock(handler))
    store = TokenStore(tmp_path / "oauth.json")
    store.save(
        "openai",
        {
            "provider": "openai",
            "access_token": "at-2",
            "refresh_token": "rt-2",
            "expires_at": time.time() - 10,
            "api_key": "sk-derived",
        },
    )
    refreshed = await oauth.refresh_tokens(store, "openai")
    assert refreshed["access_token"] == "at-3" and refreshed["refresh_token"] == "rt-2"
    assert refreshed["api_key"] == "sk-derived"
    assert await oauth.refresh_tokens(store, "gemini") is None

    config = LLMConfig("openai", "", "", "", "oauth", str(store.path))
    assert config.enabled and config.uses_oauth and config.describe()["auth"] == "oauth"
    entry = await config.oauth_entry()
    assert entry["access_token"] == "at-3"
    missing = LLMConfig("gemini", "", "", "", "oauth", str(store.path))
    with pytest.raises(LLMError) as error:
        await missing.oauth_entry()
    assert error.value.status == 401
    assert LLMConfig("openrouter", "", "", "", "oauth").enabled is False
    settings = Settings(_env_file=None, llm_provider="gemini", llm_auth="oauth")
    assert settings.llm_enabled and settings.oauth_store_path.name == "oauth.json"
    store.save(
        "gemini",
        {"provider": "gemini", "access_token": "g", "expires_at": None, "project": "p1"},
    )
    gemini = LLMConfig("gemini", "", "", "", "oauth", str(store.path))
    models = await list_models(gemini)
    assert models[0]["id"] == "gemini-4-argon" and len(models) >= 5
    assert [m["id"] for m in models][:3] == [
        "gemini-4-argon",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    ]
    assert all(spec.client_id for spec in OAUTH_PROVIDERS.values())


async def test_gemini_project_detection_and_assignment(tmp_path, monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        seen.append({"path": request.url.path, "body": payload})
        if request.url.path.endswith(":loadCodeAssist"):
            return httpx.Response(
                200,
                json={
                    "allowedTiers": [
                        {
                            "id": "standard-tier",
                            "isDefault": True,
                            "userDefinedCloudaicompanionProject": True,
                        }
                    ],
                    "ineligibleTiers": [
                        {
                            "reasonCode": "UNSUPPORTED_CLIENT",
                            "reasonMessage": "This client is no longer supported",
                        }
                    ],
                },
            )
        if request.url.path.endswith(":onboardUser"):
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {
                        "cloudaicompanionProject": {"id": payload["cloudaicompanionProject"]}
                    },
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(oauth, "_http", _mock(handler))
    info = await oauth._gemini_project("tok")
    assert info["needs_project"] is True and "no longer supported" in info["note"]
    assert info["tier"] == "standard-tier" and info["project"] == ""
    store = TokenStore(tmp_path / "oauth.json")
    with pytest.raises(LLMError):
        await oauth.set_gemini_project(store, "my-proj")
    store.save(
        "gemini",
        {"provider": "gemini", "access_token": "tok", "expires_at": None, "needs_project": True},
    )
    described = await oauth.set_gemini_project(store, "my-proj")
    assert described["project"] == "my-proj" and described["needs_project"] is False
    assert any(
        s["path"].endswith(":onboardUser") and s["body"]["tierId"] == "standard-tier" for s in seen
    )
    with pytest.raises(LLMError):
        await oauth.set_gemini_project(store, "  ")
