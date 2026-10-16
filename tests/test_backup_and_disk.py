import json

import httpx
import pytest
from conftest import make_service, make_settings

from second_brain import github_api
from second_brain.github_api import GitHubError, backup_repo_name, mask_token, push_url


def _mock(handler):
    def factory(token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=github_api.GITHUB_API,
            transport=httpx.MockTransport(handler),
            headers={"Authorization": f"Bearer {token}"},
        )

    return factory


def test_backup_names_and_masking():
    assert backup_repo_name("acme-corp") == "acme-corp_brain"
    assert backup_repo_name("x/y z") == "x-y-z_brain"
    assert (
        push_url("tok", "me/acme-corp_brain")
        == "https://x-access-token:tok@github.com/me/acme-corp_brain.git"
    )
    assert mask_token("ghp_abcdefghijklmnopqrstuvwxyz") == "ghp_abc...wxyz"
    assert mask_token("") == ""


async def test_github_api_verify_and_create(monkeypatch):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/user":
            if request.headers["Authorization"] != "Bearer good":
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(
                200,
                json={"login": "octo", "name": "Octo", "html_url": "https://github.com/octo"},
                headers={"x-oauth-scopes": "repo, delete_repo"},
            )
        if request.method == "POST" and request.url.path == "/user/repos":
            body = json.loads(request.content)
            if body["name"] == "taken_brain":
                return httpx.Response(
                    422,
                    json={
                        "message": "Repository creation failed.",
                        "errors": [{"message": "name already exists on this account"}],
                    },
                )
            return httpx.Response(
                201,
                json={
                    "full_name": f"octo/{body['name']}",
                    "html_url": f"https://github.com/octo/{body['name']}",
                    "clone_url": "x",
                    "private": True,
                    "default_branch": "main",
                },
            )
        if request.url.path == "/repos/octo/taken_brain":
            return httpx.Response(
                200,
                json={
                    "full_name": "octo/taken_brain",
                    "html_url": "https://github.com/octo/taken_brain",
                    "private": True,
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(github_api, "_client", _mock(handler))
    account = await github_api.verify_token("good")
    assert account["login"] == "octo" and "repo" in account["scopes"]
    with pytest.raises(GitHubError) as error:
        await github_api.verify_token("bad")
    assert error.value.status == 401
    created = await github_api.create_repo("good", "acme-corp_brain", "backup")
    assert created["created"] is True and created["full_name"] == "octo/acme-corp_brain"
    reused = await github_api.create_repo("good", "taken_brain", "backup")
    assert reused["created"] is False and reused["full_name"] == "octo/taken_brain"
    await github_api.delete_repo("good", "octo/acme-corp_brain")
    assert ("DELETE", "/repos/octo/acme-corp_brain") in calls


async def test_quick_project_with_backup_and_repo(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octo", "name": "Octo", "html_url": ""})
        if request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "full_name": f"octo/{body['name']}",
                    "html_url": f"https://github.com/octo/{body['name']}",
                    "private": True,
                    "default_branch": "main",
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "nope"})

    monkeypatch.setattr(github_api, "_client", _mock(handler))
    settings = make_settings(
        tmp_path, github_token="good", github_owner="octo", git_push_enabled=False
    )
    service = await make_service(settings)
    try:
        result = await service.quick_project(
            "Acme Corp", repo_url="https://github.com/acme/app", backup=True
        )
        project = result["project"]
        assert (
            project["project_id"] == "acme-corp"
            and project["backup_repo"] == "octo/acme-corp_brain"
        )
        assert project["backup_url"] == "https://github.com/octo/acme-corp_brain"
        assert (
            result["github"]["repo_url"] == "https://github.com/acme/app"
            and result["github"]["webhook_secret"]
        )
        stored = await service.get_project("acme-corp")
        assert stored.git_remote_url.startswith(
            "https://x-access-token:good@github.com/octo/acme-corp_brain.git"
        )
        assert "good" not in json.dumps(service.project_view(stored))
        recent = await service.recent_log(stored, 10)
        assert any("Backup repository" in e["summary"] for e in recent)
        no_account = await make_service(make_settings(tmp_path / "other"))
        try:
            other = await no_account.quick_project("Beta", backup=True)
            assert other["backup"] is None and "GitHub account" in other["backup_error"]
        finally:
            await no_account.stop()
            await no_account.db.close()
        outcome = await service.delete_project("acme-corp", delete_backup=True)
        assert outcome["backup_deleted"] is True and not await service.project_exists("acme-corp")
    finally:
        await service.stop()
        await service.db.close()


async def test_disk_edits_are_committed_logged_and_broadcast(tmp_path):
    settings = make_settings(tmp_path, git_push_enabled=False)
    service = await make_service(settings)
    try:
        project = await service.create_project("acme-corp", "Acme Corp")
        await service.git.drain()
        assert await service.sync_disk_changes() == []
        path = service.vault(project).root / "COMPANY_PULSE.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Disk Note\n\nedited by hand\n",
            encoding="utf-8",
        )
        assert await service.sync_disk_changes() == ["acme-corp"]
        recent = await service.recent_log(project, 5)
        assert recent[0]["type"] == "human_pull" and "disk edit" in recent[0]["summary"]
        assert await service.sync_disk_changes() == []
        index = await service.vault(project).index()
        pulse = next(f for f in index if f["file"] == "COMPANY_PULSE.md")
        assert any(s["heading"] == "Disk Note" for s in pulse["sections"])
    finally:
        await service.stop()
        await service.db.close()
