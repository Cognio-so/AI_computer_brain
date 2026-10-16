from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .llm import LLMError

TIMEOUT = 45.0
CHATGPT_BACKEND = "https://chatgpt.com/backend-api/codex/responses"
REFRESH_MARGIN = 120
STORE_FILE = "oauth.json"


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    key: str
    label: str
    account_label: str
    authorize_url: str
    token_url: str
    client_id: str
    scopes: tuple[str, ...]
    redirect_uri: str
    client_secret: str = ""
    extra_authorize: dict[str, str] = field(default_factory=dict)
    local_port: int = 0
    paste_hint: str = ""
    token_form: bool = False
    products: str = ""


OAUTH_PROVIDERS: dict[str, OAuthProvider] = {
    "anthropic": OAuthProvider(
        "anthropic",
        "Anthropic",
        "Claude account (Pro, Max, Team or Enterprise)",
        "https://claude.ai/oauth/authorize",
        "https://console.anthropic.com/v1/oauth/token",
        "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        ("org:create_api_key", "user:profile", "user:inference"),
        "https://console.anthropic.com/oauth/code/callback",
        extra_authorize={"code": "true"},
        paste_hint="After signing in, Anthropic shows a code; paste it here",
        products="the same sign-in Claude Code and Hermes use",
    ),
    "openai": OAuthProvider(
        "openai",
        "OpenAI",
        "ChatGPT account (Plus, Pro, Team or Enterprise)",
        "https://auth.openai.com/oauth/authorize",
        "https://auth.openai.com/oauth/token",
        "app_EMoamEEZ73f0CkXaXp7hrann",
        ("openid", "profile", "email", "offline_access"),
        "http://localhost:1455/auth/callback",
        extra_authorize={"id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"},
        local_port=1455,
        paste_hint="If the browser cannot reach this server, paste the full address it was sent to",
        token_form=True,
        products="the same sign-in the Codex CLI and Hermes use",
    ),
    "gemini": OAuthProvider(
        "gemini",
        "Google",
        "Google account (Gemini Code Assist)",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        (
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ),
        "http://localhost:1456/oauth2callback",
        client_secret="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",
        extra_authorize={"access_type": "offline", "prompt": "consent"},
        local_port=1456,
        paste_hint="If the browser cannot reach this server, paste the full address it was sent to",
        token_form=True,
        products="the same sign-in the Gemini CLI uses",
    ),
}


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, provider: str) -> dict[str, Any] | None:
        entry = self.load().get(provider)
        return dict(entry) if isinstance(entry, dict) else None

    def save(self, provider: str, tokens: dict[str, Any]) -> None:
        data = self.load()
        data[provider] = tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def remove(self, provider: str) -> None:
        data = self.load()
        if provider in data:
            del data[provider]
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def describe_tokens(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not entry:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "account": entry.get("account") or "",
        "expires_at": entry.get("expires_at"),
        "has_refresh": bool(entry.get("refresh_token")),
        "api_key_derived": bool(entry.get("api_key")),
        "signed_in_at": entry.get("signed_in_at"),
        "project": entry.get("project") or "",
        "needs_project": bool(entry.get("needs_project")),
        "note": entry.get("note") or "",
    }


class OAuthFlow:
    def __init__(self, store: TokenStore) -> None:
        self.store = store
        self.provider: OAuthProvider | None = None
        self.verifier = ""
        self.state = ""
        self.url = ""
        self.server: asyncio.AbstractServer | None = None
        self.captured: asyncio.Future[str] | None = None
        self.started_at: float | None = None

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider.key if self.provider else None,
            "url": self.url or None,
            "listening": self.server is not None,
            "started_at": self.started_at,
        }

    async def start(self, provider_key: str, try_local_server: bool = True) -> dict[str, Any]:
        provider = OAUTH_PROVIDERS.get(provider_key)
        if provider is None:
            raise LLMError(f"no account sign-in is available for {provider_key}", 400)
        await self.cancel()
        self.provider = provider
        self.verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(self.verifier.encode()).digest())
        self.state = _b64url(secrets.token_bytes(24))
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "scope": " ".join(provider.scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            **provider.extra_authorize,
        }
        self.url = f"{provider.authorize_url}?{urlencode(params)}"
        self.started_at = time.time()
        if provider.local_port and try_local_server:
            await self._listen(provider.local_port)
        return {
            **self.status(),
            "paste_hint": provider.paste_hint,
            "account_label": provider.account_label,
        }

    async def wait_for_callback(self, wait_seconds: float = 300) -> str | None:
        if self.captured is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(self.captured), timeout=wait_seconds)
        except TimeoutError:
            return None

    async def complete(self, pasted: str) -> dict[str, Any]:
        provider = self.provider
        if provider is None:
            raise LLMError("start the sign-in first", 409)
        code, state = _extract_code(pasted.strip())
        if not code:
            raise LLMError("paste the code or the full address the browser was sent to", 400)
        if state and self.state and state != self.state and provider.key != "anthropic":
            raise LLMError("the sign-in state does not match; start the sign-in again", 400)
        body: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": provider.redirect_uri,
            "client_id": provider.client_id,
            "code_verifier": self.verifier,
        }
        if provider.key == "anthropic":
            body["state"] = state or self.state
        if provider.client_secret:
            body["client_secret"] = provider.client_secret
        tokens = await _token_request(provider, body)
        entry = _entry_from_tokens(provider, tokens, previous=None)
        if provider.key == "openai":
            entry["api_key"] = await _exchange_openai_api_key(provider, entry)
        elif provider.key == "gemini":
            entry["account"] = await _google_account(entry["access_token"])
            info = await _gemini_project(entry["access_token"])
            entry["project"] = info["project"]
            entry["needs_project"] = info["needs_project"]
            entry["note"] = info["note"]
            entry["tier"] = info["tier"]
        self.store.save(provider.key, entry)
        await self.cancel()
        return describe_tokens(entry)

    async def cancel(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None
        if self.captured is not None and not self.captured.done():
            self.captured.cancel()
        self.captured = None
        self.url = ""

    async def _listen(self, port: int) -> None:
        loop = asyncio.get_running_loop()
        self.captured = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            except TimeoutError:
                writer.close()
                return
            target = request_line.decode("latin-1").split(" ")[1] if b" " in request_line else "/"
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
            query = parse_qs(urlparse(target).query)
            code = (query.get("code") or [""])[0]
            message = (
                "Signed in. You can close this tab and return to the AI Computer Brain."
                if code
                else "No code received. Return to the AI Computer Brain and paste the address."
            )
            body = f"<html><body style='font-family:sans-serif;padding:40px'><h2>{message}</h2></body></html>"
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}"
                ).encode()
            )
            await writer.drain()
            writer.close()
            if code and self.captured is not None and not self.captured.done():
                self.captured.set_result(target)

        try:
            self.server = await asyncio.start_server(handle, "127.0.0.1", port)
        except OSError:
            self.server = None
            self.captured = None


def _extract_code(pasted: str) -> tuple[str, str]:
    if pasted.startswith("http://") or pasted.startswith("https://") or pasted.startswith("/"):
        query = parse_qs(urlparse(pasted).query)
        return (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    if "#" in pasted:
        code, _, state = pasted.partition("#")
        return code.strip(), state.strip()
    return pasted, ""


async def _token_request(provider: OAuthProvider, body: dict[str, str]) -> dict[str, Any]:
    try:
        async with _http() as http:
            if provider.token_form:
                response = await http.post(provider.token_url, data=body)
            else:
                response = await http.post(provider.token_url, json=body)
    except httpx.HTTPError as error:
        raise LLMError(f"could not reach {provider.label}: {error}", 502) from error
    if response.status_code >= 400:
        raise LLMError(
            f"{provider.label} rejected the sign-in ({response.status_code}): {response.text[:200]}",
            401,
        )
    try:
        data = response.json()
    except ValueError as error:
        raise LLMError(f"{provider.label} returned an unexpected sign-in response", 502) from error
    if not isinstance(data, dict) or not data.get("access_token"):
        raise LLMError(f"{provider.label} returned no access token", 502)
    return data


def _entry_from_tokens(
    provider: OAuthProvider, tokens: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    expires_in = tokens.get("expires_in")
    entry: dict[str, Any] = dict(previous or {})
    entry.update(
        {
            "provider": provider.key,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or entry.get("refresh_token", ""),
            "expires_at": time.time() + float(expires_in) if expires_in else None,
            "signed_in_at": entry.get("signed_in_at") or time.time(),
        }
    )
    if tokens.get("id_token"):
        entry["id_token"] = tokens["id_token"]
        claims = _jwt_payload(str(tokens["id_token"]))
        entry["account"] = str(claims.get("email") or entry.get("account") or "")
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            entry["account_id"] = str(auth_claims.get("chatgpt_account_id") or "")
    if provider.key == "anthropic":
        account = tokens.get("account")
        if isinstance(account, dict):
            entry["account"] = str(account.get("email_address") or account.get("email") or "")
    return entry


async def _exchange_openai_api_key(provider: OAuthProvider, entry: dict[str, Any]) -> str:
    id_token = entry.get("id_token")
    if not id_token:
        return ""
    body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": provider.client_id,
        "requested_token": "openai-api-key",
        "subject_token": id_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
    }
    try:
        async with _http() as http:
            response = await http.post(provider.token_url, data=body)
        if response.status_code < 400:
            data = response.json()
            key = data.get("access_token") if isinstance(data, dict) else None
            return str(key or "")
    except (httpx.HTTPError, ValueError):
        pass
    return ""


async def _google_account(access_token: str) -> str:
    try:
        async with _http() as http:
            response = await http.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        data = response.json() if response.status_code < 400 else {}
    except (httpx.HTTPError, ValueError):
        return ""
    return str(data.get("email") or "") if isinstance(data, dict) else ""


CODE_ASSIST = "https://cloudcode-pa.googleapis.com/v1internal"
CODE_ASSIST_METADATA = {
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}


def _project_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


async def _gemini_project(access_token: str, project: str = "") -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    info: dict[str, Any] = {"project": "", "needs_project": False, "note": "", "tier": ""}
    body: dict[str, Any] = {"metadata": CODE_ASSIST_METADATA}
    if project:
        body["cloudaicompanionProject"] = project
    try:
        async with _http() as http:
            loaded = await http.post(f"{CODE_ASSIST}:loadCodeAssist", headers=headers, json=body)
            data = loaded.json() if loaded.status_code < 400 else {}
            if not isinstance(data, dict):
                data = {}
            existing = _project_id(data.get("cloudaicompanionProject"))
            current = data.get("currentTier")
            if existing:
                info["project"] = existing
                info["tier"] = (
                    _project_id(current) if isinstance(current, dict) else str(current or "")
                )
                return info
            allowed = [t for t in data.get("allowedTiers") or [] if isinstance(t, dict)]
            ineligible = [t for t in data.get("ineligibleTiers") or [] if isinstance(t, dict)]
            info["note"] = "; ".join(
                str(t.get("reasonMessage") or t.get("reasonCode") or "") for t in ineligible
            ).strip("; ")
            tier = next((t for t in allowed if t.get("isDefault")), allowed[0] if allowed else None)
            if tier is None:
                info["needs_project"] = True
                return info
            info["tier"] = str(tier.get("id") or "")
            if tier.get("userDefinedCloudaicompanionProject") and not project:
                info["needs_project"] = True
                return info
            onboard_body: dict[str, Any] = {
                "tierId": info["tier"],
                "metadata": CODE_ASSIST_METADATA,
            }
            if project:
                onboard_body["cloudaicompanionProject"] = project
            for _ in range(10):
                onboard = await http.post(
                    f"{CODE_ASSIST}:onboardUser", headers=headers, json=onboard_body
                )
                if onboard.status_code >= 400:
                    info["note"] = (
                        f"Google refused the onboarding ({onboard.status_code}): "
                        + onboard.text[:200]
                    )
                    info["needs_project"] = not project
                    return info
                result = onboard.json()
                if isinstance(result, dict) and result.get("done"):
                    response = result.get("response") or {}
                    found = _project_id(
                        response.get("cloudaicompanionProject")
                        if isinstance(response, dict)
                        else ""
                    )
                    info["project"] = found or project
                    return info
                await asyncio.sleep(2)
    except (httpx.HTTPError, ValueError) as error:
        info["note"] = f"could not reach Google: {error}"
    info["needs_project"] = not info["project"]
    return info


async def set_gemini_project(store: TokenStore, project: str) -> dict[str, Any]:
    entry = await refresh_tokens(store, "gemini")
    if not entry or not entry.get("access_token"):
        raise LLMError("sign in with Google first", 409)
    cleaned = (project or "").strip()
    if not cleaned:
        raise LLMError("enter the Google Cloud project ID", 400)
    info = await _gemini_project(str(entry["access_token"]), cleaned)
    if not info["project"]:
        raise LLMError(
            "Google did not accept that project for Gemini Code Assist"
            + (f": {info['note']}" if info["note"] else ""),
            403,
        )
    entry.update({"project": info["project"], "needs_project": False, "tier": info["tier"]})
    store.save("gemini", entry)
    return describe_tokens(entry)


async def refresh_tokens(store: TokenStore, provider_key: str) -> dict[str, Any] | None:
    entry = store.get(provider_key)
    provider = OAUTH_PROVIDERS.get(provider_key)
    if entry is None or provider is None:
        return None
    expires_at = entry.get("expires_at")
    if expires_at is None or float(expires_at) - time.time() > REFRESH_MARGIN:
        return entry
    if not entry.get("refresh_token"):
        return entry
    body = {
        "grant_type": "refresh_token",
        "refresh_token": str(entry["refresh_token"]),
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        body["client_secret"] = provider.client_secret
    tokens = await _token_request(provider, body)
    refreshed = _entry_from_tokens(provider, tokens, previous=entry)
    store.save(provider_key, refreshed)
    return refreshed
