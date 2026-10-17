from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from . import __version__, updates
from .anthropic_login import ant_binary, install_instructions, profile_status
from .auth import ADMIN_ROLE, Principal, extract_token
from .config import CANONICAL_FILES, Settings, update_env
from .documents import DocumentError
from .github_api import GitHubError, mask_token, verify_token
from .llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    bedrock_base_url,
    list_models,
    validate_provider,
)
from .markdown_engine import parse
from .oauth import OAUTH_PROVIDERS, TokenStore, describe_tokens, set_gemini_project
from .repo_analysis import (
    describe_github,
    github_settings,
    smart_merge,
    summarize_upload,
)
from .service import BrainError, BrainService

MASTER = Principal("*", ADMIN_ROLE, "admin", None, is_master=True)


def service_of(request: Request) -> BrainService:
    return request.app.state.brain


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def ip_allowed(address: str, networks: list[str]) -> bool:
    if not networks:
        return True
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in networks:
        try:
            if candidate in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def guard_admin_origin(request: Request) -> BrainService:
    service = service_of(request)
    if not ip_allowed(client_ip(request), service.settings.admin_allowed_networks):
        raise HTTPException(status_code=403, detail="admin access is not allowed from this address")
    return service


async def require_master(request: Request) -> BrainService:
    service = guard_admin_origin(request)
    settings = service.settings
    bucket = f"admin-fail:{client_ip(request)}"
    if not service.rate_limiter.peek(bucket, settings.admin_login_failures_per_minute):
        raise HTTPException(
            status_code=429,
            detail="too many failed admin attempts from this address; wait a minute",
        )
    token = extract_token({k.lower(): v for k, v in request.headers.items()}, request.url.query)
    if not token or not service.auth.is_master(token):
        service.rate_limiter.allow(bucket, settings.admin_login_failures_per_minute)
        raise HTTPException(
            status_code=401, detail="master key required", headers={"WWW-Authenticate": "Bearer"}
        )
    return service


class ProjectCreate(BaseModel):
    project_id: str = Field(min_length=3, max_length=40)
    project_name: str = Field(default="", max_length=120)
    git_remote_url: str | None = None


class ProjectSettings(BaseModel):
    project_name: str | None = None
    git_remote_url: str | None = None
    git_branch: str | None = None
    webhook_secret: str | None = None
    settings: dict[str, Any] | None = None


class KeyCreate(BaseModel):
    department_role: str = Field(min_length=3, max_length=40)
    agent_id: str = Field(min_length=3, max_length=40)
    label: str | None = Field(default=None, max_length=120)


class FileWrite(BaseModel):
    content: str
    compact_summary: str = Field(min_length=3, max_length=400)


class SectionWrite(FileWrite):
    mode: str = "replace"


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class QuickKey(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_id: str | None = None
    project_name: str | None = Field(default=None, max_length=120)
    role: str | None = None
    agent_id: str | None = None


admin = APIRouter(prefix="/api/admin", tags=["admin"])
pages = APIRouter(tags=["pages"])
agent = APIRouter(tags=["agents"])


@admin.get("/status")
async def status(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    projects = await service.list_projects()
    return {
        "ok": True,
        "version": __version__,
        "update": await asyncio.to_thread(updates.check, Path.cwd() / "data"),
        "public_url": service.settings.public_url,
        "projects": len(projects),
        "setup_required": not projects,
        "redis": service.settings.redis_enabled,
        "database": service.db.dialect,
        "stale_after_minutes": service.settings.stale_after_minutes,
        "theme": service.settings.brain_theme or "ocean",
        "admin_path": service.settings.admin_path,
        "github_account": {
            "connected": bool(service.settings.github_token),
            "owner": service.settings.github_owner,
        },
        "llm": LLMConfig.from_settings(service.settings).describe(),
    }


@admin.get("/projects")
async def list_projects(service: BrainService = Depends(require_master)) -> list[dict[str, Any]]:
    return [await service.project_overview(p) for p in await service.list_projects()]


@admin.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreate, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.create_project(
        body.project_id.strip().casefold(), body.project_name, body.git_remote_url
    )
    return await service.project_overview(project)


class QuickProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    first_agent: str | None = Field(default=None, max_length=120)
    repo_url: str | None = Field(default=None, max_length=300)
    backup: bool = False


class GitHubAccount(BaseModel):
    token: str = Field(min_length=1, max_length=400)


class MasterKeyChange(BaseModel):
    master_key: str = Field(min_length=16, max_length=400)


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    access: list[str] | str | None = None


def _document_error(error: DocumentError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.detail)


@admin.post("/projects/quick", status_code=201)
async def quick_project(
    body: QuickProject, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    return await service.quick_project(body.name, body.first_agent, body.repo_url, body.backup)


@admin.get("/github-account")
async def github_account(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    settings = service.settings
    return {
        "connected": bool(settings.github_token),
        "owner": settings.github_owner,
        "token_hint": mask_token(settings.github_token),
    }


@admin.put("/github-account")
async def connect_github_account(
    body: GitHubAccount, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    try:
        account = await verify_token(body.token)
    except GitHubError as error:
        raise HTTPException(status_code=error.status, detail=error.detail) from error
    settings.github_token = body.token.strip()
    settings.github_owner = account["login"]
    _persist_env(settings, GITHUB_TOKEN=settings.github_token, GITHUB_OWNER=settings.github_owner)
    return {
        "connected": True,
        "owner": account["login"],
        "name": account["name"],
        "scopes": account["scopes"],
        "token_hint": mask_token(settings.github_token),
    }


@admin.delete("/github-account")
async def disconnect_github_account(
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    settings = service.settings
    settings.github_token = ""
    settings.github_owner = ""
    _persist_env(settings, GITHUB_TOKEN="", GITHUB_OWNER="")
    return {"connected": False, "owner": "", "token_hint": ""}


@admin.post("/projects/{project_id}/backup", status_code=201)
async def create_backup(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    backup = await service.create_backup_repo(project)
    return {
        "backup": backup,
        "project": await service.project_overview(await service.get_project(project_id)),
    }


@admin.get("/projects/{project_id}/documents")
async def list_documents(
    project_id: str, service: BrainService = Depends(require_master)
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return [d.to_dict() for d in await service.documents.list(project)]


@admin.post("/projects/{project_id}/documents", status_code=201)
async def add_document(
    project_id: str,
    upload: UploadFile = File(...),
    title: str = Form(default=""),
    access: str = Form(default="*"),
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    data = await upload.read()
    try:
        document = await service.documents.add(
            project, data, upload.filename or "document.txt", title or None, access
        )
    except DocumentError as error:
        raise _document_error(error) from error
    return document.to_dict()


@admin.get("/projects/{project_id}/documents/{doc_id}")
async def get_document(
    project_id: str, doc_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    try:
        document = await service.documents.get(project, doc_id)
    except DocumentError as error:
        raise _document_error(error) from error
    data = document.to_dict()
    data["preview"] = (await service.documents.text(project, document))[:6000]
    return data


@admin.put("/projects/{project_id}/documents/{doc_id}")
async def update_document(
    project_id: str,
    doc_id: str,
    body: DocumentUpdate,
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    try:
        document = await service.documents.update(project, doc_id, body.title, body.access)
    except DocumentError as error:
        raise _document_error(error) from error
    return document.to_dict()


@admin.delete("/projects/{project_id}/documents/{doc_id}")
async def delete_document(
    project_id: str, doc_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    try:
        document = await service.documents.delete(project, doc_id)
    except DocumentError as error:
        raise _document_error(error) from error
    return {"deleted": document.doc_id}


@admin.get("/projects/{project_id}/documents/{doc_id}/download")
async def download_document(
    project_id: str, doc_id: str, service: BrainService = Depends(require_master)
) -> FileResponse:
    project = await service.get_project(project_id)
    try:
        document = await service.documents.get(project, doc_id)
    except DocumentError as error:
        raise _document_error(error) from error
    path = service.documents.folder(project) / document.file_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the stored file is missing")
    return FileResponse(path, media_type=document.media_type, filename=document.original_name)


@admin.get("/projects/{project_id}")
async def get_project(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    return await service.project_overview(await service.get_project(project_id))


@admin.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    delete_backup: bool = Query(default=False),
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    return await service.delete_project(project_id, delete_backup)


@admin.put("/projects/{project_id}/settings")
async def update_settings(
    project_id: str, body: ProjectSettings, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    await service.get_project(project_id)
    project = await service.update_project(project_id, **body.model_dump(exclude_unset=True))
    return await service.project_overview(project)


@admin.post("/projects/{project_id}/webhook-secret")
async def rotate_webhook_secret(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    secret = secrets.token_urlsafe(32)
    project = await service.update_project(project_id, webhook_secret=secret)
    return {
        "webhook_secret": secret,
        "webhook_url": f"{service.settings.public_url}/webhooks/{project.project_id}/github",
    }


@admin.get("/projects/{project_id}/roles")
async def roles(project_id: str, service: BrainService = Depends(require_master)) -> list[str]:
    return await service.roles(await service.get_project(project_id))


@admin.get("/projects/{project_id}/keys")
async def list_keys(
    project_id: str, service: BrainService = Depends(require_master)
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return [
        k.to_dict(service.settings.stale_after_minutes) for k in await service.list_keys(project)
    ]


@admin.post("/projects/{project_id}/keys", status_code=201)
async def create_key(
    project_id: str, body: KeyCreate, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    record, plaintext = await service.issue_key(
        project,
        body.department_role.strip().casefold(),
        body.agent_id.strip().casefold(),
        body.label,
    )
    return {
        "key": record.to_dict(service.settings.stale_after_minutes),
        "plaintext": plaintext,
        "mcp_url": f"{service.settings.public_url}/mcp",
        "snippets": service.connection_snippets(plaintext),
    }


@admin.post("/keys/quick", status_code=201)
async def quick_key(
    body: QuickKey, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    return await service.quick_key(
        body.name, body.project_id, body.project_name, body.role, body.agent_id
    )


@admin.delete("/projects/{project_id}/keys/{key_id}")
async def revoke_key(
    project_id: str, key_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return (await service.revoke_key(project, key_id)).to_dict(service.settings.stale_after_minutes)


@admin.get("/projects/{project_id}/keys/{key_id}/connect")
async def key_connect(
    project_id: str, key_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    record = await service.find_key(project, key_id)
    return service.connect_info(project, record)


@admin.post("/projects/{project_id}/keys/{key_id}/reissue", status_code=201)
async def key_reissue(
    project_id: str, key_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return await service.reissue_key(project, key_id)


@admin.post("/keys/reseal")
async def reseal_keys(
    body: MasterKeyChange, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    return {"resealed": await service.reseal_keys(body.master_key)}


@admin.get("/projects/{project_id}/files")
async def list_files(
    project_id: str, service: BrainService = Depends(require_master)
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return await service.vault(project).index()


@admin.get("/projects/{project_id}/files/{file_name}")
async def read_file(
    project_id: str, file_name: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return await service.read_file(MASTER, project, file_name, None)


@admin.put("/projects/{project_id}/files/{file_name}")
async def write_file(
    project_id: str,
    file_name: str,
    body: FileWrite,
    service: BrainService = Depends(require_master),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    result = await service.update_file(
        MASTER, project, file_name, body.content, body.compact_summary, _etag(if_match)
    )
    return result.to_dict()


@admin.get("/projects/{project_id}/files/{file_name}/sections/{slug}")
async def read_section(
    project_id: str, file_name: str, slug: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return await service.read_section(MASTER, project, file_name, slug, None)


@admin.put("/projects/{project_id}/files/{file_name}/sections/{slug}")
async def write_section(
    project_id: str,
    file_name: str,
    slug: str,
    body: SectionWrite,
    service: BrainService = Depends(require_master),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    result = await service.update_section(
        MASTER,
        project,
        file_name,
        slug,
        body.content,
        body.compact_summary,
        body.mode,
        _etag(if_match),
    )
    return result.to_dict()


@admin.get("/projects/{project_id}/log")
async def log(
    project_id: str,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return await service.admin_log(project, since, limit)


@admin.get("/projects/{project_id}/log/recent")
async def recent_log(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    service: BrainService = Depends(require_master),
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return await service.recent_log(project, limit)


@admin.get("/projects/{project_id}/log/{log_id}/diff")
async def diff(
    project_id: str, log_id: int, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return await service.diff(project, log_id)


@admin.get("/projects/{project_id}/commits")
async def commits(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    service: BrainService = Depends(require_master),
) -> list[dict[str, str]]:
    await service.get_project(project_id)
    repo = await service.repo_for(project_id)
    if repo is None or not await repo.is_repo():
        return []
    return await repo.recent_commits(limit)


@admin.get("/projects/{project_id}/sessions")
async def sessions(
    project_id: str, service: BrainService = Depends(require_master)
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return [s.to_dict() for s in await service.live_sessions(project.project_id)]


@admin.get("/projects/{project_id}/proposals")
async def proposals(
    project_id: str,
    status: str | None = Query(default="pending"),
    service: BrainService = Depends(require_master),
) -> list[dict[str, Any]]:
    project = await service.get_project(project_id)
    return [p.to_dict() for p in await service.list_proposals(project, status or None)]


@admin.post("/projects/{project_id}/proposals/{proposal_id}/approve")
async def approve(
    project_id: str,
    proposal_id: str,
    body: Decision | None = None,
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return (
        await service.decide_proposal(project, proposal_id, True, body.note if body else None)
    ).to_dict()


@admin.post("/projects/{project_id}/proposals/{proposal_id}/reject")
async def reject(
    project_id: str,
    proposal_id: str,
    body: Decision | None = None,
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return (
        await service.decide_proposal(project, proposal_id, False, body.note if body else None)
    ).to_dict()


@admin.get("/projects/{project_id}/export")
async def export_vault(
    project_id: str, service: BrainService = Depends(require_master)
) -> Response:
    project = await service.get_project(project_id)
    data = await service.vaults.export_zip(project.project_id)
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project.project_id}-vault.zip"'},
    )


@admin.post("/projects/{project_id}/import")
async def import_vault(
    project_id: str,
    archive: UploadFile = File(...),
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    written = await service.vaults.import_zip(project.project_id, await archive.read())
    service.vaults.vault_for(project.project_id)._policy_cache = None
    await service.system_event(
        project.project_id, "vault_import", f"imported {len(written)} files from archive"
    )
    return {"written": written}


class LLMSettings(BaseModel):
    provider: str = Field(default="", max_length=20)
    api_key: str | None = Field(default=None, max_length=400)
    model: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=300)
    auth: str | None = Field(default=None, max_length=20)
    effort: str | None = Field(default=None, max_length=20)
    clear_key: bool = False


class LoginCode(BaseModel):
    code: str = Field(min_length=1, max_length=400)
    model: str | None = Field(default=None, max_length=120)


class OAuthStart(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    local_server: bool = True


class OAuthComplete(BaseModel):
    code: str = Field(default="", max_length=4000)
    model: str | None = Field(default=None, max_length=120)


class GeminiProject(BaseModel):
    project: str = Field(min_length=1, max_length=120)


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=40)


class ChatApply(BaseModel):
    changes: list[dict[str, Any]] = Field(min_length=1, max_length=12)


class LLMModelsRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    api_key: str | None = Field(default=None, max_length=400)
    base_url: str | None = Field(default=None, max_length=300)
    region: str | None = Field(default=None, max_length=40)
    auth: str | None = Field(default=None, max_length=20)


class GitHubConnect(BaseModel):
    repo_url: str = Field(min_length=1, max_length=300)
    token: str | None = Field(default=None, max_length=300)
    branch: str | None = Field(default=None, max_length=120)
    analyze_on_push: bool | None = None
    clear_token: bool = False


def _llm_error(error: LLMError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.detail)


def _persist_env(settings: Settings, **values: str) -> None:
    path = settings.env_path()
    if path is not None:
        update_env(path, **values)


@admin.get("/llm")
async def llm_settings(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    return LLMConfig.from_settings(service.settings).describe()


@admin.put("/llm")
async def save_llm(
    body: LLMSettings, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    try:
        provider = validate_provider(body.provider)
    except LLMError as error:
        raise _llm_error(error) from error
    api_key = settings.llm_api_key
    if body.clear_key or not provider:
        api_key = ""
    elif body.api_key is not None and body.api_key.strip():
        api_key = body.api_key.strip()
    auth = (body.auth or settings.llm_auth).strip().casefold()
    if auth == "login" and provider != "anthropic":
        raise HTTPException(status_code=400, detail="the CLI login is available for Anthropic only")
    if auth == "oauth" and provider not in OAUTH_PROVIDERS:
        raise HTTPException(
            status_code=400, detail="account sign-in is available for OpenAI, Anthropic and Gemini"
        )
    if auth in ("login", "oauth"):
        api_key = ""
    settings.llm_auth = auth if auth in ("login", "oauth") else "api_key"
    settings.llm_provider = provider
    settings.llm_api_key = api_key
    if body.model is not None:
        settings.llm_model = body.model.strip()
    if body.base_url is not None:
        settings.llm_base_url = body.base_url.strip()
    if body.effort is not None:
        settings.llm_effort = body.effort.strip().casefold()
    if not provider:
        settings.llm_model = ""
        settings.llm_base_url = ""
        settings.llm_effort = ""
    _persist_env(
        settings,
        LLM_PROVIDER=settings.llm_provider,
        LLM_API_KEY=settings.llm_api_key,
        LLM_MODEL=settings.llm_model,
        LLM_BASE_URL=settings.llm_base_url,
        LLM_AUTH=settings.llm_auth,
        LLM_EFFORT=settings.llm_effort,
    )
    return LLMConfig.from_settings(settings).describe()


@admin.get("/llm/login")
async def llm_login_status(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    return {
        "available": ant_binary() is not None,
        "install": install_instructions(),
        "profile": await profile_status(),
        "flow": service.login.status(),
        "active": LLMConfig.from_settings(service.settings).uses_login,
    }


@admin.post("/llm/login/start")
async def llm_login_start(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    try:
        return await service.login.start()
    except LLMError as error:
        raise _llm_error(error) from error


@admin.post("/llm/login/complete")
async def llm_login_complete(
    body: LoginCode, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    try:
        await service.login.submit_code(body.code)
        config = LLMConfig("anthropic", "", body.model or "", "", "login")
        models = await list_models(config)
    except LLMError as error:
        raise _llm_error(error) from error
    settings.llm_provider = "anthropic"
    settings.llm_auth = "login"
    settings.llm_api_key = ""
    settings.llm_base_url = ""
    chosen = (body.model or "").strip() or settings.llm_model
    if not chosen or chosen not in {m["id"] for m in models}:
        chosen = models[0]["id"]
    settings.llm_model = chosen
    _persist_env(
        settings,
        LLM_PROVIDER="anthropic",
        LLM_API_KEY="",
        LLM_MODEL=chosen,
        LLM_BASE_URL="",
        LLM_AUTH="login",
    )
    result = LLMConfig.from_settings(settings).describe()
    result["models"] = models
    return result


@admin.get("/llm/oauth")
async def oauth_status(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    store = TokenStore(service.settings.oauth_store_path)
    return {
        "providers": {
            key: {
                "label": spec.label,
                "account_label": spec.account_label,
                "products": spec.products,
                **describe_tokens(store.get(key)),
            }
            for key, spec in OAUTH_PROVIDERS.items()
        },
        "flow": service.oauth.status(),
        "active": LLMConfig.from_settings(service.settings).uses_oauth,
    }


@admin.post("/llm/oauth/start")
async def oauth_start(
    body: OAuthStart, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    try:
        return await service.oauth.start(body.provider.strip().casefold(), body.local_server)
    except LLMError as error:
        raise _llm_error(error) from error


@admin.post("/llm/oauth/wait")
async def oauth_wait(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    captured = await service.oauth.wait_for_callback(wait_seconds=25)
    return {"received": captured is not None, "value": captured}


@admin.post("/llm/oauth/complete")
async def oauth_complete(
    body: OAuthComplete, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    flow = service.oauth
    provider = flow.provider.key if flow.provider else ""
    if not provider:
        raise HTTPException(status_code=409, detail="start the sign-in first")
    pasted = body.code.strip()
    if not pasted:
        captured = await flow.wait_for_callback(wait_seconds=1)
        if not captured:
            raise HTTPException(
                status_code=400, detail="paste the code or the address the browser was sent to"
            )
        pasted = captured
    try:
        account = await flow.complete(pasted)
        config = LLMConfig(
            provider, "", body.model or "", "", "oauth", str(settings.oauth_store_path)
        )
        models = await list_models(config)
    except LLMError as error:
        raise _llm_error(error) from error
    settings.llm_provider = provider
    settings.llm_auth = "oauth"
    settings.llm_api_key = ""
    settings.llm_base_url = ""
    chosen = (body.model or "").strip() or settings.llm_model
    if not chosen or chosen not in {m["id"] for m in models}:
        chosen = models[0]["id"]
    settings.llm_model = chosen
    _persist_env(
        settings,
        LLM_PROVIDER=provider,
        LLM_API_KEY="",
        LLM_MODEL=chosen,
        LLM_BASE_URL="",
        LLM_AUTH="oauth",
    )
    result = LLMConfig.from_settings(settings).describe()
    result["models"] = models
    result["account"] = account
    return result


@admin.post("/llm/oauth/gemini-project")
async def oauth_gemini_project(
    body: GeminiProject, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    try:
        account = await set_gemini_project(TokenStore(settings.oauth_store_path), body.project)
        config = LLMConfig(
            "gemini", "", settings.llm_model, "", "oauth", str(settings.oauth_store_path)
        )
        check = await LLMClient(config).ping()
    except LLMError as error:
        raise _llm_error(error) from error
    return {"account": account, "check": check}


@admin.delete("/llm/oauth/{provider}")
async def oauth_sign_out(
    provider: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    TokenStore(settings.oauth_store_path).remove(provider.casefold())
    if settings.llm_provider == provider.casefold() and settings.llm_auth == "oauth":
        settings.llm_provider = ""
        settings.llm_auth = "api_key"
        settings.llm_model = ""
        _persist_env(settings, LLM_PROVIDER="", LLM_MODEL="", LLM_AUTH="api_key")
    return {"signed_out": provider.casefold()}


@admin.post("/projects/{project_id}/chat")
async def chat(
    project_id: str, body: ChatRequest, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    if not LLMConfig.from_settings(service.settings).enabled:
        raise HTTPException(status_code=409, detail="connect an LLM first (GitHub & LLM page)")
    try:
        return await service.assistant.respond(project, body.messages)
    except LLMError as error:
        raise _llm_error(error) from error


@admin.post("/projects/{project_id}/chat/apply")
async def chat_apply(
    project_id: str, body: ChatApply, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    results = await service.assistant.apply(project, body.changes)
    return {"results": results, "applied": sum(1 for r in results if r["ok"])}


@admin.post("/llm/models")
async def llm_models(
    body: LLMModelsRequest, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    settings = service.settings
    try:
        provider = validate_provider(body.provider)
    except LLMError as error:
        raise _llm_error(error) from error
    api_key = (body.api_key or "").strip()
    if not api_key and settings.llm_provider == provider:
        api_key = settings.llm_api_key
    base_url = (body.base_url or "").strip()
    if provider == "bedrock":
        base_url = bedrock_base_url(body.region or "")
    elif not base_url and settings.llm_provider == provider:
        base_url = settings.llm_base_url
    auth = (body.auth or "").strip().casefold()
    if not auth and settings.llm_provider == provider:
        auth = settings.llm_auth
    config = LLMConfig(
        provider,
        api_key,
        "",
        base_url,
        auth if auth in ("login", "oauth") else "api_key",
        str(settings.oauth_store_path),
    )
    try:
        models = await list_models(config)
    except LLMError as error:
        raise _llm_error(error) from error
    spec = config.spec
    return {
        "provider": provider,
        "base_url": config.resolved_base_url,
        "default": spec.default_model if spec else "",
        "models": models,
    }


@admin.post("/llm/test")
async def test_llm(service: BrainService = Depends(require_master)) -> dict[str, Any]:
    try:
        return await LLMClient(LLMConfig.from_settings(service.settings)).ping()
    except LLMError as error:
        raise _llm_error(error) from error


@admin.get("/projects/{project_id}/github")
async def github_status(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    data = describe_github(project, service.settings.public_url)
    data["analysis"] = service.analyzer.status(project.project_id)
    data["llm_enabled"] = LLMConfig.from_settings(service.settings).enabled
    return data


@admin.put("/projects/{project_id}/github")
async def github_connect(
    project_id: str, body: GitHubConnect, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    project, generated_secret = await service.connect_github(
        project, body.repo_url, body.token, body.branch, body.analyze_on_push, body.clear_token
    )
    result = describe_github(project, service.settings.public_url)
    result["analysis"] = service.analyzer.status(project.project_id)
    result["llm_enabled"] = LLMConfig.from_settings(service.settings).enabled
    if generated_secret:
        result["webhook_secret"] = generated_secret
    return result


@admin.delete("/projects/{project_id}/github")
async def github_disconnect(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    project = await service.update_project(project.project_id, settings={"github": {}})
    return describe_github(project, service.settings.public_url)


@admin.post("/projects/{project_id}/github/analyze", status_code=202)
async def github_analyze(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    if not github_settings(project).get("repo_url"):
        raise HTTPException(status_code=409, detail="connect a GitHub repository first")
    if not LLMConfig.from_settings(service.settings).enabled:
        raise HTTPException(status_code=409, detail="connect an LLM provider first")
    started = service.analyzer.start(project.project_id, "manual")
    return {"started": started, "analysis": service.analyzer.status(project.project_id)}


@admin.get("/projects/{project_id}/github/analysis")
async def github_analysis(
    project_id: str, service: BrainService = Depends(require_master)
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    return service.analyzer.status(project.project_id)


@admin.post("/projects/{project_id}/files/{file_name}/upload")
async def upload_file(
    project_id: str,
    file_name: str,
    upload: UploadFile = File(...),
    mode: str = Form(default="replace"),
    compact_summary: str = Form(default=""),
    service: BrainService = Depends(require_master),
) -> dict[str, Any]:
    project = await service.get_project(project_id)
    raw = await upload.read()
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="the file is larger than 2 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=400, detail="upload a UTF-8 Markdown file") from error
    if not text.strip():
        raise HTTPException(status_code=400, detail="the uploaded file is empty")
    chosen = (mode or "replace").strip().casefold()
    original = upload.filename or "upload.md"
    summary = compact_summary.strip() or summarize_upload(file_name, original, chosen)
    if chosen == "smart":
        try:
            current = await service.read_file(MASTER, project, file_name, None)
            text = await smart_merge(
                LLMConfig.from_settings(service.settings), file_name, current["content"], text
            )
        except LLMError as error:
            raise _llm_error(error) from error
    elif chosen != "replace":
        raise HTTPException(status_code=400, detail="mode must be replace or smart")
    if not parse(text).sections:
        raise HTTPException(
            status_code=400,
            detail="the Markdown needs at least one ## heading to become a brain file",
        )
    result = await service.update_file(MASTER, project, file_name, text, summary, None)
    data = result.to_dict()
    data["mode"] = chosen
    data["bytes"] = len(raw)
    return data


@admin.get("/projects/{project_id}/events")
async def admin_events(
    project_id: str, request: Request, service: BrainService = Depends(require_master)
) -> StreamingResponse:
    project = await service.get_project(project_id)
    return _sse(service, project.project_id, request, None)


@agent.get("/events")
async def agent_events(request: Request) -> StreamingResponse:
    service = service_of(request)
    principal: Principal = request.state.principal
    project = await service.resolve(principal, request.query_params.get("project_id"))
    return _sse(service, project.project_id, request, principal)


@pages.get("/healthz", include_in_schema=False)
async def healthz(request: Request) -> dict[str, Any]:
    service = service_of(request)
    return {"ok": await service.db.ping(), "version": __version__}


async def dashboard(request: Request) -> FileResponse:
    service = guard_admin_origin(request)
    return FileResponse(
        service.settings.admin_dashboard_path,
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


def register_pages(app: FastAPI, settings: Settings) -> None:
    async def root() -> RedirectResponse:
        return RedirectResponse(settings.admin_path, status_code=307)

    app.add_api_route("/", root, include_in_schema=False)
    app.add_api_route(settings.admin_path, dashboard, include_in_schema=False)


def _etag(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip('"').removeprefix("W/").strip('"') or None


def _sse(
    service: BrainService, project_id: str, request: Request, principal: Principal | None
) -> StreamingResponse:
    session_id = f"events-{secrets.token_hex(8)}"
    heartbeat = service.settings.heartbeat_seconds

    async def stream() -> AsyncIterator[bytes]:
        if principal is not None:
            await service.touch_session(
                principal, "events-sse", session_id, request.headers.get("user-agent")
            )
        yield f"event: ready\ndata: {json.dumps({'project_id': project_id})}\n\n".encode()
        try:
            async with service.hub.subscribe(project_id) as queue:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                    except TimeoutError:
                        if principal is not None:
                            await service.touch_session(principal, "events-sse", session_id)
                        yield b": ping\n\n"
                        continue
                    yield event.sse().encode("utf-8")
        finally:
            if principal is not None:
                service.spawn(service.close_session(session_id))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def brain_error_handler(_: Request, error: BrainError) -> JSONResponse:
    return JSONResponse(error.to_dict(), status_code=error.status)


def validate_markdown(content: str) -> list[dict[str, Any]]:
    return parse(content).index()


__all__ = [
    "admin",
    "pages",
    "agent",
    "brain_error_handler",
    "register_pages",
    "CANONICAL_FILES",
    "validate_markdown",
]
