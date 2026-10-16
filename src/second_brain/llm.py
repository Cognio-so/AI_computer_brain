from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import LLM_PROVIDERS, Settings

REQUEST_TIMEOUT = 600.0
PING_TIMEOUT = 60.0
OAUTH_CAPABLE = ("anthropic", "openai", "gemini")
EFFORT_OPTIONS: dict[str, tuple[str, ...]] = {
    "anthropic": ("low", "medium", "high", "xhigh", "max"),
    "default": ("low", "medium", "high"),
}
OAUTH_BETA = "oauth-2025-04-20"
CATALOG_DATE = "2026-10-16"
STATIC_MODELS: dict[str, list[tuple[str, str]]] = {
    "openai": [
        ("gpt-6-astra", "most capable, for the most demanding work"),
        ("gpt-6.1-sol", "near-Astra performance at a lower cost"),
        ("gpt-6-luna", "most efficient, for focused high-volume tasks"),
        ("gpt-5.5", "previous flagship"),
        ("gpt-5.3-codex", "coding-tuned"),
    ],
    "anthropic": [
        ("claude-opus-5-5", "recommended for most workloads"),
        ("claude-fable-5-1", "most capable, for demanding reasoning and long agentic work"),
        ("claude-sonnet-5-5", "best balance of speed and intelligence"),
        ("claude-haiku-5-5", "fastest and cheapest"),
        ("claude-opus-5", "legacy"),
        ("claude-sonnet-5", "legacy"),
    ],
    "gemini": [
        ("gemini-4-argon", "flagship reasoning model, 1M context"),
        ("gemini-3.8-flash", "most intelligent Flash model"),
        ("gemini-3.6-flash", "previous Flash generation"),
        ("gemini-3.5-flash-lite", "fastest and cheapest"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro"),
        ("gemini-3.1-flash-lite", "frontier-class at low cost"),
        ("gemini-3-flash-preview", "preview"),
        ("gemini-2.5-pro", "limited access"),
        ("gemini-2.5-flash", "limited access"),
    ],
}
GEMINI_PUBLIC_MODELS = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_PROJECT_HINT = (
    "Google no longer offers the free Gemini sign-in to tools other than its own ({note}). Your "
    "Google sign-in works, but Gemini needs either a Google Cloud project with a Gemini Code "
    "Assist licence (set it with /llm or on the GitHub & LLM page) or a free API key from "
    "https://aistudio.google.com/apikey"
)


def catalog_models(provider: str) -> list[dict[str, str]]:
    return [{"id": model_id, "name": note} for model_id, note in STATIC_MODELS.get(provider, [])]


FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n|\n```\s*$")


class LLMError(RuntimeError):
    def __init__(self, detail: str, status: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    base_url: str
    default_model: str
    key_hint: str
    docs_url: str
    needs_base_url: bool = False
    needs_region: bool = False
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "gpt-6.1-sol",
        "sk-...",
        "https://platform.openai.com/api-keys",
    ),
    "anthropic": Provider(
        "anthropic",
        "Anthropic (Claude)",
        "https://api.anthropic.com",
        "claude-opus-5-5",
        "sk-ant-...",
        "https://console.anthropic.com/settings/keys",
    ),
    "gemini": Provider(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-4-argon",
        "AIza...",
        "https://aistudio.google.com/apikey",
    ),
    "openrouter": Provider(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "anthropic/claude-opus-5.5",
        "sk-or-...",
        "https://openrouter.ai/keys",
        note="one key for hundreds of models",
    ),
    "bedrock": Provider(
        "bedrock",
        "Amazon Bedrock",
        "",
        "",
        "Bedrock API key",
        "https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html",
        needs_region=True,
        note="Bedrock API key plus the AWS region",
    ),
    "custom": Provider(
        "custom",
        "Other (OpenAI-compatible API)",
        "",
        "",
        "API key",
        "",
        needs_base_url=True,
        note="Ollama, LM Studio, vLLM, Groq, Mistral, Together and similar",
    ),
}


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]}"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    auth: str = "api_key"
    token_store: str = ""
    effort: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMConfig:
        return cls(
            settings.llm_provider,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_base_url,
            settings.llm_auth,
            str(settings.oauth_store_path),
            settings.llm_effort,
        )

    @property
    def effort_options(self) -> list[str]:
        return list(EFFORT_OPTIONS.get(self.provider, EFFORT_OPTIONS["default"]))

    @property
    def resolved_effort(self) -> str:
        value = (self.effort or "").strip().casefold()
        if value in ("", "default") or value not in self.effort_options:
            return ""
        return value

    def provider_effort(self) -> str:
        value = self.resolved_effort
        if not value:
            return ""
        if self.provider == "anthropic":
            return value
        return "high" if value in ("xhigh", "max") else value

    @property
    def uses_login(self) -> bool:
        return self.provider == "anthropic" and self.auth == "login"

    @property
    def uses_oauth(self) -> bool:
        return self.auth == "oauth" and self.provider in OAUTH_CAPABLE

    async def oauth_entry(self) -> dict[str, Any]:
        from .oauth import TokenStore, refresh_tokens

        if not self.token_store:
            raise LLMError("no token store configured for the account sign-in", 500)
        entry = await refresh_tokens(TokenStore(Path(self.token_store)), self.provider)
        if not entry or not entry.get("access_token"):
            raise LLMError(
                f"no {self.provider} account is signed in on this server; sign in again or use an API key",
                401,
            )
        return entry

    @property
    def spec(self) -> Provider | None:
        return PROVIDERS.get(self.provider)

    @property
    def resolved_model(self) -> str:
        spec = self.spec
        return self.model.strip() or (spec.default_model if spec else "")

    @property
    def resolved_base_url(self) -> str:
        spec = self.spec
        return (self.base_url.strip() or (spec.base_url if spec else "")).rstrip("/")

    @property
    def enabled(self) -> bool:
        if self.provider not in PROVIDERS:
            return False
        if not self.uses_login and not self.uses_oauth and not self.api_key.strip():
            return False
        if not self.resolved_model:
            return False
        return bool(self.resolved_base_url)

    def describe(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "provider": self.provider,
            "provider_label": spec.label if spec else "",
            "model": self.resolved_model,
            "base_url": self.resolved_base_url,
            "key_set": bool(self.api_key),
            "key_hint": mask_key(self.api_key),
            "auth": "login" if self.uses_login else ("oauth" if self.uses_oauth else "api_key"),
            "effort": self.resolved_effort or "default",
            "effort_options": ["default", *self.effort_options],
            "enabled": self.enabled,
            "providers": [
                {
                    "key": p.key,
                    "label": p.label,
                    "default_model": p.default_model,
                    "base_url": p.base_url,
                    "key_hint": p.key_hint,
                    "docs_url": p.docs_url,
                    "needs_base_url": p.needs_base_url,
                    "needs_region": p.needs_region,
                    "note": p.note,
                    "oauth": p.key in OAUTH_CAPABLE,
                }
                for p in PROVIDERS.values()
            ],
        }


def validate_provider(provider: str) -> str:
    cleaned = (provider or "").strip().casefold()
    if cleaned and cleaned not in LLM_PROVIDERS:
        raise LLMError(f"provider must be one of {', '.join(LLM_PROVIDERS)}", 400)
    return cleaned


def extract_json(text: str) -> dict[str, Any]:
    cleaned = FENCE_RE.sub("", text.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError("the model did not return a JSON object", 502)
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as error:
        raise LLMError(f"the model returned invalid JSON: {error}", 502) from error
    if not isinstance(data, dict):
        raise LLMError("the model returned JSON that is not an object", 502)
    return data


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        if not config.enabled:
            raise LLMError("no LLM is connected; add a provider and API key first", 409)
        self.config = config

    async def complete(self, system: str, user: str, max_tokens: int = 16000) -> str:
        if self.config.provider == "anthropic":
            return await self._anthropic(system, user, max_tokens)
        if self.config.uses_oauth and self.config.provider == "gemini":
            return await self._gemini_code_assist(system, user, max_tokens)
        if self.config.uses_oauth and self.config.provider == "openai":
            entry = await self.config.oauth_entry()
            if entry.get("api_key"):
                return await self._openai_compatible(system, user, max_tokens, entry["api_key"])
            return await self._chatgpt_backend(entry, system, user, max_tokens)
        return await self._openai_compatible(system, user, max_tokens)

    async def _gemini_code_assist(self, system: str, user: str, max_tokens: int) -> str:
        from .oauth import CODE_ASSIST

        entry = await self.config.oauth_entry()
        if not entry.get("project"):
            raise LLMError(GEMINI_PROJECT_HINT.format(note=entry.get("note") or ""), 403)
        body = {
            "model": self.config.resolved_model,
            "project": entry.get("project") or "",
            "request": {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        }
        headers = {"Authorization": f"Bearer {entry['access_token']}"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
            response = await self._post(http, f"{CODE_ASSIST}:generateContent", headers, body)
            if response.status_code == 403:
                fallback = await self._post(
                    http,
                    f"{GEMINI_PUBLIC_MODELS}/{self.config.resolved_model}:generateContent",
                    {**headers, "x-goog-user-project": str(entry.get("project") or "")},
                    body["request"],
                )
                if fallback.status_code == 200:
                    response = fallback
        if response.status_code == 401:
            raise LLMError("Google rejected the sign-in; sign in again", 401)
        if response.status_code == 403:
            raise LLMError(
                f"Google refused ({_error_reason(response)}): {_error_text(response)} "
                f"[project {entry.get('project')}]. Use a Google Cloud project with a Gemini Code "
                "Assist licence and the Generative Language API enabled, or an API key from "
                "https://aistudio.google.com/apikey",
                403,
            )
        if response.status_code >= 400:
            raise LLMError(f"Gemini error {response.status_code}: {_error_text(response)}", 502)
        try:
            data = response.json()
            payload = data.get("response", data)
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError("unexpected response shape from Gemini", 502) from error
        if not text.strip():
            raise LLMError("the model returned an empty answer", 502)
        return text

    async def _chatgpt_backend(
        self, entry: dict[str, Any], system: str, user: str, max_tokens: int
    ) -> str:
        from .oauth import CHATGPT_BACKEND

        headers = {
            "Authorization": f"Bearer {entry['access_token']}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        }
        if entry.get("account_id"):
            headers["chatgpt-account-id"] = str(entry["account_id"])
        body = {
            "model": self.config.resolved_model,
            "instructions": system,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user}],
                }
            ],
            "store": False,
            "stream": True,
        }
        if self.config.provider_effort():
            body["reasoning"] = {"effort": self.config.provider_effort()}
        text_parts: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
                async with http.stream(
                    "POST", CHATGPT_BACKEND, headers=headers, json=body
                ) as response:
                    if response.status_code in (401, 403):
                        raise LLMError("OpenAI rejected the sign-in; sign in again", 401)
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")[:300]
                        raise LLMError(f"OpenAI error {response.status_code}: {detail}", 502)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        if event.get("type") == "response.output_text.delta":
                            text_parts.append(str(event.get("delta") or ""))
                        elif event.get("type") == "response.completed" and not text_parts:
                            output = event.get("response", {}).get("output", [])
                            for item in output:
                                for part in item.get("content", []) or []:
                                    if part.get("type") == "output_text":
                                        text_parts.append(str(part.get("text") or ""))
        except httpx.HTTPError as error:
            raise LLMError(f"could not reach OpenAI: {error}", 502) from error
        text = "".join(text_parts)
        if not text.strip():
            raise LLMError("the model returned an empty answer", 502)
        return text

    async def ping(self) -> dict[str, Any]:
        started = time.monotonic()
        reply = await self.complete(
            "You are a connectivity check. Answer with the single word OK.", "Reply with OK.", 32
        )
        return {
            "ok": True,
            "model": self.config.resolved_model,
            "provider": self.config.provider,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "reply": reply.strip()[:80],
        }

    async def _anthropic(self, system: str, user: str, max_tokens: int) -> str:
        try:
            import anthropic
        except ImportError as error:
            raise LLMError(
                "install the anthropic package to use Claude: pip install anthropic", 500
            ) from error
        client = anthropic.AsyncAnthropic(
            **await _anthropic_credentials(self.config),
            base_url=self.config.resolved_base_url or None,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
        )
        extra: dict[str, Any] = {}
        if self.config.provider_effort():
            extra["output_config"] = {"effort": self.config.provider_effort()}
        try:
            async with client.messages.stream(
                model=self.config.resolved_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **extra,
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as error:
            raise LLMError("Anthropic rejected the API key", 401) from error
        except anthropic.NotFoundError as error:
            raise LLMError(f"model {self.config.resolved_model!r} was not found", 404) from error
        except anthropic.RateLimitError as error:
            raise LLMError("Anthropic rate limit reached, try again shortly", 429) from error
        except anthropic.APIStatusError as error:
            raise LLMError(f"Anthropic error {error.status_code}: {error.message}", 502) from error
        except anthropic.APIConnectionError as error:
            raise LLMError(f"could not reach Anthropic: {error}", 502) from error
        except anthropic.AnthropicError as error:
            raise LLMError(_login_hint(self.config, error), 401) from error
        finally:
            await client.close()
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            explanation = getattr(details, "explanation", None) or "the model declined the request"
            raise LLMError(f"Claude refused: {explanation}", 422)
        text = "".join(block.text for block in message.content if block.type == "text")
        if message.stop_reason == "max_tokens":
            raise LLMError(
                "the answer was cut off at max_tokens; retry with a smaller repository digest", 502
            )
        if not text.strip():
            raise LLMError("the model returned an empty answer", 502)
        return text

    async def _openai_compatible(
        self, system: str, user: str, max_tokens: int, api_key: str | None = None
    ) -> str:
        url = f"{self.config.resolved_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key or self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self.config.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/ai-computer-brain/ai-computer-brain"
            headers["X-Title"] = "AI Computer Brain"
        body: dict[str, Any] = {
            "model": self.config.resolved_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
        }
        if self.config.provider_effort():
            body["reasoning_effort"] = self.config.provider_effort()
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
            response = await self._post(http, url, headers, body)
            if response.status_code == 400 and "max_completion_tokens" in response.text:
                body["max_completion_tokens"] = body.pop("max_tokens")
                response = await self._post(http, url, headers, body)
            if (
                response.status_code == 400
                and "reasoning_effort" in body
                and "reasoning" in response.text.casefold()
            ):
                body.pop("reasoning_effort")
                response = await self._post(http, url, headers, body)
        if response.status_code == 401:
            raise LLMError("the provider rejected the API key", 401)
        if response.status_code == 404:
            raise LLMError(f"model {self.config.resolved_model!r} or endpoint not found", 404)
        if response.status_code == 429:
            raise LLMError("provider rate limit reached, try again shortly", 429)
        if response.status_code >= 400:
            raise LLMError(f"provider error {response.status_code}: {_error_text(response)}", 502)
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError("unexpected response shape from the provider", 502) from error
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = str(content or "")
        if choice.get("finish_reason") == "length":
            raise LLMError(
                "the answer was cut off at max_tokens; retry with a smaller repository digest", 502
            )
        if not text.strip():
            raise LLMError("the model returned an empty answer", 502)
        return text

    @staticmethod
    async def _post(
        http: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> httpx.Response:
        try:
            return await http.post(url, headers=headers, json=body)
        except httpx.HTTPError as error:
            raise LLMError(f"could not reach the provider: {error}", 502) from error


def _error_reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return "error"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                return str(detail["reason"])
        return str(error.get("status") or "error")
    return "error"


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or data)[:300]


CHAT_MODEL_EXCLUDE = (
    "embedding",
    "embed",
    "audio",
    "realtime",
    "tts",
    "transcribe",
    "whisper",
    "dall-e",
    "image",
    "moderation",
    "search",
    "instruct",
    "imagen",
    "veo",
    "aqa",
    "babbage",
    "davinci",
    "computer-use",
    "rerank",
)
MODEL_LIST_TIMEOUT = 45.0


def _is_chat_model(provider: str, model_id: str) -> bool:
    lowered = model_id.casefold()
    if any(token in lowered for token in CHAT_MODEL_EXCLUDE):
        return False
    if provider == "openai":
        return lowered.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
    if provider == "gemini":
        return "gemini" in lowered or "gemma" in lowered
    return True


def _model_entry(provider: str, item: dict[str, Any]) -> dict[str, str] | None:
    model_id = str(item.get("id") or item.get("name") or "").strip()
    if provider == "gemini" and model_id.startswith("models/"):
        model_id = model_id[len("models/") :]
    if not model_id or not _is_chat_model(provider, model_id):
        return None
    name = str(item.get("name") or item.get("display_name") or model_id)
    if provider == "gemini" and name.startswith("models/"):
        name = name[len("models/") :]
    return {"id": model_id, "name": name if name != model_id else ""}


def rank_models(provider: str, models: list[dict[str, str]]) -> list[dict[str, str]]:
    spec = PROVIDERS.get(provider)
    default = spec.default_model if spec else ""
    unique: dict[str, dict[str, str]] = {}
    for model in models:
        unique.setdefault(model["id"], model)
    known = [model_id for model_id, _ in STATIC_MODELS.get(provider, [])]
    ordered = sorted(
        unique.values(),
        key=lambda m: (
            known.index(m["id"]) if m["id"] in known else len(known),
            m["id"].casefold(),
        ),
    )
    if default in unique:
        ordered.remove(unique[default])
        ordered.insert(0, unique[default])
    return ordered


async def list_models(config: LLMConfig) -> list[dict[str, str]]:
    spec = config.spec
    if spec is None:
        raise LLMError("choose a provider first", 400)
    if (
        not config.api_key.strip()
        and config.provider != "custom"
        and not config.uses_login
        and not config.uses_oauth
    ):
        raise LLMError("enter the API key first", 400)
    if not config.resolved_base_url:
        raise LLMError("enter the API base URL first", 400)
    if config.provider == "anthropic":
        return rank_models("anthropic", await _anthropic_models(config))
    api_key = config.api_key
    if config.uses_oauth:
        entry = await config.oauth_entry()
        api_key = str(entry.get("api_key") or "")
        if not api_key:
            if config.provider == "gemini":
                live = await _gemini_models_with_token(
                    str(entry["access_token"]), str(entry.get("project") or "")
                )
                if live:
                    return rank_models("gemini", live)
            return rank_models(config.provider, catalog_models(config.provider))
    url = f"{config.resolved_base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=MODEL_LIST_TIMEOUT) as http:
            response = await http.get(url, headers=headers)
    except httpx.HTTPError as error:
        raise LLMError(f"could not reach the provider: {error}", 502) from error
    if response.status_code in (401, 403):
        raise LLMError("the provider rejected the API key", 401)
    if response.status_code == 404:
        raise LLMError("this endpoint does not list models; type the model id instead", 404)
    if response.status_code >= 400:
        raise LLMError(f"provider error {response.status_code}: {_error_text(response)}", 502)
    try:
        payload = response.json()
    except ValueError as error:
        raise LLMError("the provider did not return a model list", 502) from error
    items = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and not isinstance(items, list):
        items = payload.get("models")
    if not isinstance(items, list):
        raise LLMError("the provider did not return a model list", 502)
    models = [
        entry
        for entry in (_model_entry(config.provider, i) for i in items if isinstance(i, dict))
        if entry
    ]
    if not models:
        raise LLMError("the provider returned no chat models for this key", 404)
    return rank_models(config.provider, models)


async def _gemini_models_with_token(access_token: str, project: str) -> list[dict[str, str]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if project:
        headers["x-goog-user-project"] = project
    try:
        async with httpx.AsyncClient(timeout=MODEL_LIST_TIMEOUT) as http:
            response = await http.get(
                GEMINI_PUBLIC_MODELS, params={"pageSize": 200}, headers=headers
            )
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    try:
        items = response.json().get("models", [])
    except (ValueError, AttributeError):
        return []
    models: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        entry = _model_entry("gemini", {"id": item.get("name"), "name": item.get("displayName")})
        if entry:
            models.append(entry)
    return models


async def _anthropic_models(config: LLMConfig) -> list[dict[str, str]]:
    try:
        import anthropic
    except ImportError as error:
        raise LLMError(
            "install the anthropic package to use Claude: pip install anthropic", 500
        ) from error
    client = anthropic.AsyncAnthropic(
        **await _anthropic_credentials(config),
        base_url=config.resolved_base_url or None,
        timeout=MODEL_LIST_TIMEOUT,
        max_retries=1,
    )
    models: list[dict[str, str]] = []
    try:
        async for model in client.models.list(limit=100):
            models.append({"id": model.id, "name": model.display_name or ""})
    except anthropic.AuthenticationError as error:
        raise LLMError("Anthropic rejected the API key", 401) from error
    except anthropic.APIStatusError as error:
        raise LLMError(f"Anthropic error {error.status_code}: {error.message}", 502) from error
    except anthropic.APIConnectionError as error:
        raise LLMError(f"could not reach Anthropic: {error}", 502) from error
    except anthropic.AnthropicError as error:
        raise LLMError(_login_hint(config, error), 401) from error
    finally:
        await client.close()
    if not models:
        raise LLMError("Anthropic returned no models for this key", 404)
    return models


async def _anthropic_credentials(config: LLMConfig) -> dict[str, Any]:
    if config.uses_login:
        return {}
    if config.uses_oauth:
        entry = await config.oauth_entry()
        return {
            "auth_token": entry["access_token"],
            "default_headers": {"anthropic-beta": OAUTH_BETA},
        }
    return {"api_key": config.api_key}


def _login_hint(config: LLMConfig, error: Exception) -> str:
    if config.uses_login or config.uses_oauth:
        return (
            "the Anthropic sign-in on this server is missing or expired; sign in again "
            f"or use an API key: {str(error)[:160]}"
        )
    return f"Anthropic error: {str(error)[:200]}"


def bedrock_base_url(region: str) -> str:
    cleaned = (region or "").strip().casefold() or "us-east-1"
    return f"https://bedrock-runtime.{cleaned}.amazonaws.com/openai/v1"
