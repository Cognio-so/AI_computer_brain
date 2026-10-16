from __future__ import annotations

import re
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
TIMEOUT = 30.0
NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class GitHubError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def backup_repo_name(project_id: str) -> str:
    return NAME_RE.sub("-", f"{project_id}_brain").strip("-") or "brain"


def push_url(token: str, full_name: str) -> str:
    return f"https://x-access-token:{token}@github.com/{full_name}.git"


def mask_token(token: str) -> str:
    if not token:
        return ""
    return f"{token[:7]}...{token[-4:]}" if len(token) > 14 else "*" * len(token)


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GITHUB_API,
        timeout=TIMEOUT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-computer-brain",
        },
    )


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        errors = data.get("errors")
        extra = ""
        if isinstance(errors, list) and errors:
            first = errors[0]
            extra = (
                f" ({first.get('message') or first.get('code')})" if isinstance(first, dict) else ""
            )
        return f"{data.get('message') or response.text[:200]}{extra}"
    return response.text[:200]


async def _request(token: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        async with _client(token) as http:
            return await http.request(method, path, **kwargs)
    except httpx.HTTPError as error:
        raise GitHubError(502, f"could not reach GitHub: {error}") from error


async def verify_token(token: str) -> dict[str, Any]:
    cleaned = (token or "").strip()
    if not cleaned:
        raise GitHubError(400, "enter a GitHub token")
    response = await _request(cleaned, "GET", "/user")
    if response.status_code == 401:
        raise GitHubError(401, "GitHub rejected the token")
    if response.status_code >= 400:
        raise GitHubError(502, f"GitHub error {response.status_code}: {_detail(response)}")
    data = response.json()
    scopes = response.headers.get("x-oauth-scopes", "")
    return {
        "login": str(data.get("login") or ""),
        "name": str(data.get("name") or ""),
        "html_url": str(data.get("html_url") or ""),
        "scopes": [s.strip() for s in scopes.split(",") if s.strip()],
    }


async def create_repo(
    token: str, name: str, description: str, private: bool = True
) -> dict[str, Any]:
    body = {
        "name": name,
        "description": description[:300],
        "private": private,
        "auto_init": False,
        "has_issues": False,
        "has_projects": False,
        "has_wiki": False,
    }
    response = await _request(token, "POST", "/user/repos", json=body)
    if response.status_code == 201:
        data = response.json()
        return _repo_info(data, created=True)
    if response.status_code == 422 and "already exists" in response.text:
        account = await verify_token(token)
        existing = await _request(token, "GET", f"/repos/{account['login']}/{name}")
        if existing.status_code == 200:
            return _repo_info(existing.json(), created=False)
    if response.status_code in (401, 403):
        raise GitHubError(
            401,
            "the GitHub token may not create repositories; it needs the repo scope "
            "(classic) or Administration and Contents write access (fine-grained)",
        )
    raise GitHubError(502, f"GitHub error {response.status_code}: {_detail(response)}")


async def delete_repo(token: str, full_name: str) -> None:
    response = await _request(token, "DELETE", f"/repos/{full_name}")
    if response.status_code in (204, 404):
        return
    if response.status_code in (401, 403):
        raise GitHubError(
            403, "the GitHub token may not delete repositories (delete_repo scope needed)"
        )
    raise GitHubError(502, f"GitHub error {response.status_code}: {_detail(response)}")


def _repo_info(data: dict[str, Any], created: bool) -> dict[str, Any]:
    return {
        "full_name": str(data.get("full_name") or ""),
        "html_url": str(data.get("html_url") or ""),
        "clone_url": str(data.get("clone_url") or ""),
        "private": bool(data.get("private", True)),
        "default_branch": str(data.get("default_branch") or "main"),
        "created": created,
    }
