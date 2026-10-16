from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .anthropic_login import LoginFlow
from .auth import Authenticator, AuthError, Principal, RateLimiter
from .chat import ChatAssistant
from .company_log import CompanyLog, format_entry
from .config import (
    ACTIVITY_KINDS,
    ADMIN_ROLE,
    CANONICAL_FILES,
    CHANGELOG_FILE,
    DIGEST_DAYS,
    DIGEST_HEADING,
    IMPORTANCE_LEVELS,
    LOG_FILE,
    PERMISSIONS_FILE,
    SCHEMA_FILE,
    Settings,
    is_identifier,
)
from .database import Database
from .documents import DocumentStore
from .git_sync import GitError, GitRepo, GitWorker, mask_remote, normalize_remote, parse_github_push
from .github_api import GitHubError, backup_repo_name, create_repo, delete_repo, push_url
from .guards import clamp_summary, find_secret
from .hub import Event, LocalHub
from .locks import LockRegistry
from .markdown_engine import (
    Document,
    SectionExists,
    SectionNotFound,
    WriteMode,
    apply_write,
    normalize,
    parse,
    replace_section_tree,
    slugify,
)
from .models import AgentSession, ApiKey, MutationLog, Project, Proposal, brain_uri, iso, utcnow
from .oauth import OAuthFlow, TokenStore
from .rbac import Permission, Policy, affected_roles
from .repo_analysis import RepoAnalyzer
from .vault import FileNotAllowed, Vault, VaultManager, validate_file_name

log = logging.getLogger("brain.service")
DELIVERY_TTL_SECONDS = 300
LIVE_SESSION_SECONDS = 180
SCHEMA_TABLES_HEADING = "Tables"
SCHEMA_ENGINES_HEADING = "Engines"
SCHEMA_MIGRATIONS_HEADING = "Migration Log"
PLACEHOLDER_PREFIX = "- (no "
NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
AFFECTS_HINT = (
    "This change affects your department: read what changed, decide what in your own work must "
    "adjust, do it, then log the adjustment with log_agent_activity."
)
DIGEST_EVENT_LABELS = {
    "activity_task": "task",
    "activity_finding": "finding",
    "activity_decision": "decision",
    "activity_blocker": "blocker",
    "agent_joined": "agent",
    "agent_revoked": "agent",
    "agent_update": "update",
    "approved_update": "update",
    "admin_edit": "update",
    "db_schema": "schema",
    "human_commit": "commit",
    "human_pull": "pull",
    "document_added": "document",
    "document_updated": "document",
    "document_removed": "document",
}
GENERIC_PROTOCOL = (
    "You are connected to the AI Computer Brain, the shared memory of this project. "
    "Call whoami first in every session, then get_brain_index and read the sections your role "
    "depends on, keeping their etags. At the start of every turn call get_company_log_delta with "
    "the last id you saw and re-read only sections that changed; entries marked affects_you are "
    "work for you, adjust your output and log the adjustment. After you change something real "
    "(a price, a feature, a schema, a brand token, an endpoint, a decision) call "
    "update_brain_section with a one-line summary for other agents and the etag from your last "
    "read; on CONFLICT re-read, merge and retry once. After work that other departments must know "
    "about (a change they rely on, something shipped, something new found, a decision, a blocker) "
    "call log_agent_activity with what you did and which departments it affects; routine steps "
    "stay in your own memory. A section whose only line starts with '(no' is empty: never treat it "
    "as a fact, verify in the real source and then fill it. Before you finish a session log your "
    "findings of the day. Documents "
    "the admin uploaded are your knowledge base: list_documents shows them with a summary and "
    "topics, search_documents finds passages, read_document reads them in chunks. whoami.sources "
    "names the backup repository and the company repository. The brain is the summary, not the database: "
    "verify live details in the source system. Never write secrets into the brain."
)


class BrainError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.code, "error": self.detail, **self.extra}


@dataclass(slots=True)
class WriteResult:
    status: str
    file: str
    heading: str | None
    slug: str | None
    new_etag: str | None = None
    previous_etag: str | None = None
    log_id: int | None = None
    proposal_id: str | None = None
    file_etag: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "file": self.file,
            "heading": self.heading,
            "slug": self.slug,
            "new_etag": self.new_etag,
            "previous_etag": self.previous_etag,
            "file_etag": self.file_etag,
            "log_id": self.log_id,
        }
        if self.proposal_id:
            data["proposal_id"] = self.proposal_id
        data.update(self.extra)
        return {k: v for k, v in data.items() if v is not None}


KEY_PLACEHOLDER = "<paste the agent key>"


class BrainService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        hub: LocalHub,
        locks: LockRegistry,
        vaults: VaultManager,
        auth: Authenticator,
    ) -> None:
        self.settings = settings
        self.db = db
        self.hub = hub
        self.locks = locks
        self.vaults = vaults
        self.auth = auth
        self.rate_limiter = RateLimiter()
        self.git = GitWorker(
            db,
            settings,
            self.repo_for,
            self._files_pulled,
            self._publish_payload,
            self.system_event,
            self._attach_commit,
        )
        self._deliveries: dict[str, float] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self.analyzer = RepoAnalyzer(self)
        self.documents = DocumentStore(self)
        self.assistant = ChatAssistant(self)
        self.login = LoginFlow()
        self.oauth = OAuthFlow(TokenStore(settings.oauth_store_path))

    async def start(self) -> None:
        await self.hub.start()
        await self.adopt_vault_histories()
        await self.git.start()
        if self.settings.disk_watch_seconds > 0:
            self.spawn(self._watch_disk())

    async def stop(self) -> None:
        await self.git.stop()
        await self.hub.stop()
        for task in list(self._background):
            task.cancel()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def list_projects(self) -> list[Project]:
        async with self.db.session() as session:
            return list(await session.scalars(select(Project).order_by(Project.created_at)))

    async def get_project(self, project_id: str) -> Project:
        async with self.db.session() as session:
            project = await session.get(Project, project_id)
        if project is None:
            raise BrainError(404, "NOT_FOUND", f"project {project_id!r} does not exist")
        return project

    async def project_exists(self, project_id: str) -> bool:
        async with self.db.session() as session:
            return await session.get(Project, project_id) is not None

    async def create_project(
        self, project_id: str, project_name: str, git_remote_url: str | None = None
    ) -> Project:
        if not is_identifier(project_id):
            raise BrainError(
                400,
                "INVALID",
                "project_id must be 3-40 characters of a-z, 0-9 and hyphens, starting and ending with a letter or digit, for example acme-corp",
            )
        if await self.project_exists(project_id):
            raise BrainError(409, "CONFLICT", f"project {project_id!r} already exists")
        created = utcnow()
        vault = await self.vaults.provision(project_id, iso(created) or "")
        project = Project(
            project_id=project_id,
            project_name=project_name.strip() or project_id,
            storage_path=str(vault.root),
            git_remote_url=git_remote_url or None,
            created_at=created,
        )
        async with self.db.transaction() as session:
            session.add(project)
        repo = self.git_repo(project_id, vault.root)
        await repo.init(project.git_branch)
        await repo.commit_all(f"[brain] initialize vault for {project_id}")
        if git_remote_url:
            await repo.set_remote(git_remote_url)
        await self.system_event(project_id, "project_created", f"project {project_id} created")
        return project

    async def delete_project(self, project_id: str, delete_backup: bool = False) -> dict[str, Any]:
        project = await self.get_project(project_id)
        backup = project.settings.get("backup") if isinstance(project.settings, dict) else None
        outcome: dict[str, Any] = {
            "deleted": project_id,
            "backup_deleted": False,
            "backup_error": None,
        }
        if delete_backup and isinstance(backup, dict) and backup.get("repo"):
            if not self.settings.github_token:
                outcome["backup_error"] = "no GitHub account connected"
            else:
                try:
                    await delete_repo(self.settings.github_token, str(backup["repo"]))
                    outcome["backup_deleted"] = True
                except GitHubError as error:
                    outcome["backup_error"] = error.detail
        await self._delete_project_data(project_id)
        return outcome

    async def _delete_project_data(self, project_id: str) -> None:
        project = await self.get_project(project_id)
        async with self.db.transaction() as session:
            row = await session.get(Project, project.project_id)
            if row is not None:
                await session.delete(row)
        await self.vaults.destroy(project_id)

    async def update_project(self, project_id: str, **fields: Any) -> Project:
        async with self.db.transaction() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise BrainError(404, "NOT_FOUND", f"project {project_id!r} does not exist")
            if fields.get("project_name"):
                project.project_name = str(fields["project_name"]).strip()
            if "git_remote_url" in fields:
                project.git_remote_url = (fields["git_remote_url"] or "").strip() or None
            if fields.get("git_branch"):
                project.git_branch = str(fields["git_branch"]).strip()
            if "webhook_secret" in fields:
                project.webhook_secret = (fields["webhook_secret"] or "").strip() or None
            if isinstance(fields.get("settings"), dict):
                project.settings = {**project.settings, **fields["settings"]}
        if "git_remote_url" in fields:
            repo = await self.repo_for(project_id)
            if repo is not None:
                await repo.set_remote(project.git_remote_url)
        return project

    def project_view(self, project: Project, **extra: Any) -> dict[str, Any]:
        data = project.to_dict()
        data["git_remote_url"] = mask_remote(project.git_remote_url)
        github = data["settings"].get("github")
        if isinstance(github, dict):
            cleaned = {k: v for k, v in github.items() if k != "token"}
            cleaned["token_set"] = bool(github.get("token"))
            data["settings"] = {**data["settings"], "github": cleaned}
        data["github_connected"] = bool(isinstance(github, dict) and github.get("repo_url"))
        backup = data["settings"].get("backup")
        data["backup_repo"] = backup.get("repo") if isinstance(backup, dict) else None
        data["backup_url"] = backup.get("html_url") if isinstance(backup, dict) else None
        data.update(extra)
        return data

    async def project_overview(self, project: Project) -> dict[str, Any]:
        async with self.db.session() as session:
            keys = list(
                await session.scalars(select(ApiKey).where(ApiKey.project_id == project.project_id))
            )
            latest = await session.scalar(
                select(MutationLog)
                .where(MutationLog.project_id == project.project_id)
                .order_by(MutationLog.log_id.desc())
                .limit(1)
            )
            github = await session.scalar(
                select(MutationLog)
                .where(
                    MutationLog.project_id == project.project_id,
                    MutationLog.event_type.in_(("human_commit", "human_pull")),
                )
                .order_by(MutationLog.log_id.desc())
                .limit(1)
            )
            activity = await session.scalar(
                select(MutationLog)
                .where(
                    MutationLog.project_id == project.project_id,
                    MutationLog.event_type.like("activity_%"),
                )
                .order_by(MutationLog.log_id.desc())
                .limit(1)
            )
            pending = list(
                await session.scalars(
                    select(Proposal.proposal_id).where(
                        Proposal.project_id == project.project_id, Proposal.status == "pending"
                    )
                )
            )
        live = await self.live_sessions(project.project_id)
        active = [k for k in keys if k.active]
        stale = [k for k in active if k.is_stale(self.settings.stale_after_minutes)]
        return self.project_view(
            project,
            key_count=len(active),
            live_agents=len(live),
            pending_proposals=len(pending),
            agents_synced=len(active) - len(stale),
            agents_stale=len(stale),
            github_last_event_at=iso(github.created_at) if github else None,
            last_activity_at=iso(activity.created_at) if activity else None,
            last_mutation_at=iso(latest.created_at) if latest else None,
            last_mutation_summary=latest.compact_summary if latest else None,
        )

    def vault(self, project: Project | str) -> Vault:
        if isinstance(project, Project):
            return self.vaults.vault_for(project.project_id, project.storage_path)
        return self.vaults.vault_for(project)

    def company_log(self, project: Project | str) -> CompanyLog:
        return CompanyLog(self.db, self.vault(project), self.locks, self.settings.hot_log_size)

    async def repo_for(self, project_id: str) -> GitRepo | None:
        try:
            project = await self.get_project(project_id)
        except BrainError:
            return None
        return self.git_repo(project.project_id, self.vault(project).root)

    def git_repo(self, project_id: str, root: Path) -> GitRepo:
        return GitRepo(
            root,
            self.settings.git_author_name,
            self.settings.git_author_email,
            self.settings.brain_files_dir / ".git-store" / project_id,
        )

    async def adopt_vault_histories(self) -> None:
        for project in await self.list_projects():
            repo = self.git_repo(project.project_id, self.vault(project).root)
            try:
                await repo.init(project.git_branch)
            except GitError as error:
                log.warning("vault %s: git history unavailable: %s", project.project_id, error)

    async def policy(self, project: Project | str) -> Policy:
        return await self.vault(project).policy()

    async def roles(self, project: Project | str) -> list[str]:
        policy = await self.policy(project)
        roles = list(policy.roles)
        if ADMIN_ROLE not in roles:
            roles.insert(0, ADMIN_ROLE)
        return roles

    async def resolve(self, principal: Principal, requested: str | None) -> Project:
        try:
            project_id = principal.resolve_project(requested)
        except AuthError as error:
            code = "FORBIDDEN" if error.status == 403 else "INVALID"
            raise BrainError(error.status, code, error.detail) from error
        return await self.get_project(project_id)

    @staticmethod
    def role_of(principal: Principal) -> str:
        return ADMIN_ROLE if principal.is_master else principal.role

    def _limit(self, principal: Principal, kind: str) -> None:
        if principal.is_master:
            return
        limit = (
            self.settings.rate_limit_writes_per_minute
            if kind == "write"
            else self.settings.rate_limit_reads_per_minute
        )
        if not self.rate_limiter.allow(f"{principal.key_id}:{kind}", limit):
            raise BrainError(429, "RATE_LIMITED", f"too many {kind} calls for this key, slow down")

    async def whoami(self, principal: Principal, project: Project) -> dict[str, Any]:
        policy = await self.policy(project)
        role = self.role_of(principal)
        company_log = self.company_log(project)
        latest = await company_log.latest_id()
        writable = set(policy.writable_files(role))
        guide = [
            {"when": when, "update": target}
            for when, target in policy.file_guide
            if _guide_applies(target, writable)
        ]
        watch = policy.watch.get(role)
        today = utcnow().date()
        mine = [
            e
            for e in await company_log.entries_for_day(today)
            if e.agent_id == principal.agent_id and e.event_type.startswith("activity_")
        ]
        documents = await self.documents.visible(project, principal)
        view = self.project_view(project)
        github = view["settings"].get("github") if isinstance(view.get("settings"), dict) else {}
        return {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "role": role,
            "agent_id": principal.agent_id,
            "documents": [d.brief() for d in documents[:25]],
            "document_count": len(documents),
            "sources": {
                "backup_repo": view.get("backup_url"),
                "company_repo": (github or {}).get("repo_url")
                if isinstance(github, dict)
                else None,
                "documents_dir": "docs/",
                "note": (
                    "The brain files, the uploaded documents and the logs are mirrored to the backup "
                    "repository after every change; read it when you want Git history, but write "
                    "through this MCP server so changes are checked, logged and broadcast."
                ),
            },
            "permissions": policy.summary(role),
            "heading_permissions": policy.heading_summary(role),
            "watch": [f"{t.file_name}#{t.slug}" if t.slug else t.file_name for t in watch]
            if watch
            else list(CANONICAL_FILES),
            "cadence": policy.cadence.get(role, "every-turn"),
            "latest_log_id": latest,
            "rules": list(policy.agent_rules) or GENERIC_PROTOCOL.split(". "),
            "file_guide": guide,
            "changelog_section": f"{CHANGELOG_FILE}#{slugify(role)}",
            "today": {
                "date": today.isoformat(),
                "my_entries": len(mine),
                "findings_logged": any(e.event_type == "activity_finding" for e in mine),
            },
            "tools": self.tool_names(principal, policy),
            "files": list(CANONICAL_FILES),
        }

    def tool_names(self, principal: Principal, policy: Policy) -> list[str]:
        role = self.role_of(principal)
        tools = [
            "whoami",
            "get_brain_index",
            "read_brain_section",
            "read_brain_file",
            "update_brain_section",
            "get_company_log_delta",
            "log_agent_activity",
            "get_daily_digest",
            "list_documents",
            "search_documents",
            "read_document",
        ]
        if policy.write_permission(role, SCHEMA_FILE).writable:
            tools.append("sync_db_schema")
        if policy.has_approvals() or role == ADMIN_ROLE:
            tools.append("get_proposal")
        if principal.is_master:
            tools.append("list_projects")
        return tools

    def protocol_text(
        self, principal: Principal | None = None, policy: Policy | None = None
    ) -> str:
        if policy is None or principal is None:
            return GENERIC_PROTOCOL
        role = self.role_of(principal)
        rules = list(policy.agent_rules) or [GENERIC_PROTOCOL]
        writable = set(policy.writable_files(role))
        guide = [
            f"- {when} → {target}"
            for when, target in policy.file_guide
            if _guide_applies(target, writable)
        ]
        lines = [
            f"You are {principal.agent_id} with role {role} in project {principal.project_id}.",
            "",
            "Rules:",
        ]
        lines.extend(f"- {rule}" for rule in rules)
        if guide:
            lines.extend(["", "What to update, and where:", *guide])
        writable_text = ", ".join(sorted(writable)) or "nothing (read-only role)"
        lines.extend(
            [
                "",
                f"You may write: {writable_text}.",
                f"Your changelog lives in {CHANGELOG_FILE} under '## {role}' and is written only "
                "through log_agent_activity; the server removes duplicates and archives old entries.",
                "Documents uploaded by the admin are your knowledge base: list_documents shows the "
                "ones shared with you, search_documents finds passages, read_document reads them in "
                "chunks (offset, max_chars). Check them before answering questions about the company.",
            ]
        )
        return "\n".join(lines)

    async def index(self, principal: Principal, project: Project) -> list[dict[str, Any]]:
        self._limit(principal, "read")
        policy = await self.policy(project)
        role = self.role_of(principal)
        return [
            entry
            for entry in await self.vault(project).index()
            if policy.can_read(role, str(entry["file"]))
        ]

    async def read_section(
        self,
        principal: Principal,
        project: Project,
        file_name: str,
        heading: str,
        known_etag: str | None,
    ) -> dict[str, Any]:
        self._limit(principal, "read")
        name = self._file(file_name, allow_archive=True)
        role = self.role_of(principal)
        policy = await self.policy(project)
        doc = parse(await self.vault(project).read(name, allow_archive=True))
        section = doc.find(heading)
        if section is None:
            raise BrainError(
                404,
                "NOT_FOUND",
                f"heading {heading!r} not found in {name}",
                available=[s.heading for s in doc.sections],
            )
        if not policy.can_read(role, name, section.slug):
            raise BrainError(403, "FORBIDDEN", f"role {role} may not read {name}#{section.slug}")
        if known_etag and known_etag == section.etag:
            return {
                "status": "NOT_MODIFIED",
                "file": name,
                "heading": section.heading,
                "slug": section.slug,
                "etag": section.etag,
            }
        return {
            "status": "ok",
            "file": name,
            "heading": section.heading,
            "slug": section.slug,
            "level": section.level,
            "etag": section.etag,
            "file_etag": doc.etag,
            "content": doc.body(section),
            "children": [c.heading for c in doc.children(section)],
        }

    async def read_file(
        self, principal: Principal, project: Project, file_name: str, known_etag: str | None
    ) -> dict[str, Any]:
        self._limit(principal, "read")
        name = self._file(file_name, allow_archive=True)
        role = self.role_of(principal)
        policy = await self.policy(project)
        if not policy.can_read(role, name):
            raise BrainError(403, "FORBIDDEN", f"role {role} may not read {name}")
        text = await self.vault(project).read(name, allow_archive=True)
        doc = parse(text)
        if known_etag and known_etag == doc.etag:
            return {"status": "NOT_MODIFIED", "file": name, "etag": doc.etag}
        return {
            "status": "ok",
            "file": name,
            "etag": doc.etag,
            "content": text,
            "sections": doc.index(),
        }

    async def update_section(
        self,
        principal: Principal,
        project: Project,
        file_name: str,
        heading: str,
        content: str,
        compact_summary: str,
        mode: str = "replace",
        expected_etag: str | None = None,
        subagent_role: str | None = None,
        importance: str = "normal",
    ) -> WriteResult:
        self._limit(principal, "write")
        name = self._file(file_name)
        write_mode = self._mode(mode)
        summary = self._summary(compact_summary)
        importance = self._importance(importance)
        self._check_content(content, write_mode)
        if name == LOG_FILE:
            raise BrainError(403, "FORBIDDEN", "COMPANY_LOG.md is written by the server only")
        role = self.role_of(principal)
        policy = await self.policy(project)
        slug = slugify(_leaf(heading))
        permission = policy.write_permission(role, name, slug)
        if write_mode is WriteMode.DELETE and role != ADMIN_ROLE:
            raise BrainError(403, "FORBIDDEN", "only admin may delete sections")
        if not permission.writable:
            hint = (
                " Use log_agent_activity to write your own department's changelog."
                if name == CHANGELOG_FILE
                else ""
            )
            raise BrainError(
                403,
                "FORBIDDEN",
                f"role {role} may not write {name}#{slug}.{hint}",
                permission=policy.permission(role, name, slug).value,
            )
        if permission is Permission.WRITE_APPROVE and role != ADMIN_ROLE:
            return await self._propose(
                principal, project, name, heading, content, summary, write_mode, expected_etag
            )
        return await self._apply(
            project,
            agent_id=principal.agent_id,
            department=role,
            subagent_role=subagent_role,
            file_name=name,
            heading=heading,
            content=content,
            mode=write_mode,
            expected_etag=expected_etag,
            summary=summary,
            importance=importance,
            event_type="admin_edit" if principal.is_master else "agent_update",
        )

    async def update_file(
        self,
        principal: Principal,
        project: Project,
        file_name: str,
        content: str,
        compact_summary: str,
        expected_etag: str | None = None,
    ) -> WriteResult:
        name = self._file(file_name)
        if name == LOG_FILE:
            raise BrainError(403, "FORBIDDEN", "COMPANY_LOG.md is written by the server only")
        role = self.role_of(principal)
        policy = await self.policy(project)
        if policy.write_permission(role, name) is not Permission.WRITE:
            raise BrainError(403, "FORBIDDEN", f"role {role} may not rewrite {name}")
        if len(content.encode("utf-8")) > self.settings.max_patch_bytes * 4:
            raise BrainError(413, "TOO_LARGE", "file content exceeds the size limit")
        secret = find_secret(content)
        if secret:
            raise BrainError(
                422,
                "SECRET_DETECTED",
                f"content looks like it contains a {secret}; secrets never belong in the brain",
            )
        summary = self._summary(compact_summary)
        vault = self.vault(project)
        affects = affected_roles(policy, name, None, exclude=role)
        async with self.locks.hold(project.project_id, name):
            current = parse(await vault.read(name))
            if expected_etag and expected_etag != current.etag:
                raise BrainError(
                    409,
                    "CONFLICT",
                    "file changed since you read it",
                    current_etag=current.etag,
                    current_content=current.text,
                )
            new_doc = parse(content)
            await vault.write(name, new_doc.text)
            changed = _changed_sections(current, new_doc)
            events = [
                Event(
                    "file_updated",
                    project.project_id,
                    file=name,
                    etag=new_doc.etag,
                    previous_etag=current.etag,
                    agent_id=principal.agent_id,
                    department=role,
                    summary=summary,
                    affects=affects,
                )
            ]
            events.extend(
                Event(
                    "section_updated",
                    project.project_id,
                    file=name,
                    heading=s.heading,
                    slug=s.slug,
                    etag=s.etag,
                    agent_id=principal.agent_id,
                    department=role,
                    summary=summary,
                    affects=affects,
                )
                for s in changed
            )
            entry = await self._commit_mutation(
                project,
                event_type="admin_edit" if role == ADMIN_ROLE else "agent_update",
                agent_id=principal.agent_id,
                department=role,
                subagent_role=None,
                target_uri=brain_uri(project.project_id, name),
                summary=summary,
                importance="normal",
                previous_etag=current.etag,
                new_etag=new_doc.etag,
                events=events,
                affects=affects,
            )
        if name == PERMISSIONS_FILE:
            vault._policy_cache = None
        return WriteResult(
            "ok",
            name,
            None,
            None,
            new_doc.etag,
            current.etag,
            entry.log_id,
            file_etag=new_doc.etag,
            extra={"changed_sections": [s.slug for s in changed], "affects": affects},
        )

    async def log_activity(
        self,
        principal: Principal,
        project: Project,
        summary: str,
        kind: str = "task",
        affects: list[str] | None = None,
        details: str | None = None,
        importance: str = "normal",
        subagent_role: str | None = None,
    ) -> dict[str, Any]:
        self._limit(principal, "write")
        kind_value = (kind or "task").strip().casefold()
        if kind_value not in ACTIVITY_KINDS:
            raise BrainError(400, "INVALID", f"kind must be one of {', '.join(ACTIVITY_KINDS)}")
        summary_text = self._summary(summary)
        importance_value = self._importance(importance)
        policy = await self.policy(project)
        role = self.role_of(principal)
        affected = self._affects(policy, affects, role)
        detail_text = " ".join((details or "").split())[:300] or None
        for text in (summary_text, detail_text or ""):
            secret = find_secret(text)
            if secret:
                raise BrainError(422, "SECRET_DETECTED", f"entry looks like it contains a {secret}")
        duplicate = await self._duplicate_of(project, summary_text)
        if duplicate is not None:
            return {
                "status": "DUPLICATE",
                "message": (
                    "an equivalent entry was logged in the last "
                    f"{self.settings.changelog_dedupe_hours} hours; nothing was written"
                ),
                "existing": duplicate.compact(),
            }
        actor = principal.actor(subagent_role).split("/", 1)[1]
        line, etag = await self._append_changelog(
            project, role, actor, kind_value, summary_text, affected, detail_text
        )
        entry = await self._commit_mutation(
            project,
            event_type=f"activity_{kind_value}",
            agent_id=principal.agent_id,
            department=role,
            subagent_role=subagent_role,
            target_uri=brain_uri(project.project_id, CHANGELOG_FILE, slugify(role)),
            summary=f"[{kind_value}] {summary_text}",
            importance=importance_value,
            previous_etag=None,
            new_etag=etag,
            events=[
                Event(
                    "activity_logged",
                    project.project_id,
                    file=CHANGELOG_FILE,
                    heading=role,
                    slug=slugify(role),
                    etag=etag,
                    agent_id=principal.agent_id,
                    department=role,
                    subagent_role=subagent_role,
                    summary=f"[{kind_value}] {summary_text}",
                    importance=importance_value,
                    affects=affected,
                )
            ],
            affects=affected,
        )
        if kind_value == "finding":
            await self.company_log(project).rewrite_findings(self.settings.findings_in_company_log)
        digest_date = await self._refresh_digest(project)
        return {
            "status": "ok",
            "file": CHANGELOG_FILE,
            "section": role,
            "kind": kind_value,
            "entry": line,
            "affects": affected,
            "log_id": entry.log_id,
            "digest_date": digest_date,
        }

    async def daily_digest(
        self, principal: Principal, project: Project, day: str | None
    ) -> dict[str, Any]:
        self._limit(principal, "read")
        target = utcnow().date()
        if day:
            try:
                target = date.fromisoformat(day.strip())
            except ValueError as error:
                raise BrainError(400, "INVALID", "date must look like 2026-10-15") from error
        entries = [
            e for e in await self.company_log(project).entries_for_day(target) if _in_digest(e)
        ]
        counts: dict[str, int] = {}
        departments: list[str] = []
        for entry in entries:
            label = DIGEST_EVENT_LABELS.get(entry.event_type, "other")
            counts[label] = counts.get(label, 0) + 1
            if entry.department not in departments:
                departments.append(entry.department)
        return {
            "date": target.isoformat(),
            "counts": counts,
            "departments": departments,
            "entries": [
                {**e.compact(), "kind": DIGEST_EVENT_LABELS.get(e.event_type, "other")}
                for e in entries
            ],
        }

    async def _duplicate_of(self, project: Project, summary: str) -> MutationLog | None:
        wanted = _normalize_text(summary)
        if not wanted:
            return None
        recent = await self.company_log(project).recent_activity(
            self.settings.changelog_dedupe_hours
        )
        for entry in recent:
            if _normalize_text(_strip_kind(entry.compact_summary)) == wanted:
                return entry
        return None

    async def _append_changelog(
        self,
        project: Project,
        department: str,
        actor: str,
        kind: str,
        summary: str,
        affects: list[str],
        details: str | None = None,
    ) -> tuple[str, str | None]:
        stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
        line = f"- {stamp} · {actor} · [{kind}] {summary}"
        if affects:
            line += " · affects: " + ", ".join(affects)
        lines = [line] + ([f"  - {details}"] if details else [])
        vault = self.vault(project)
        async with self.locks.hold(project.project_id, CHANGELOG_FILE, slugify(department)):
            text = await vault.read(CHANGELOG_FILE)
            doc = parse(text)
            section = doc.find(department)
            if section is None:
                text, section = apply_write(text, department, "\n".join(lines), WriteMode.CREATE)
            else:
                merged = lines + _without_placeholder(doc.body(section)).splitlines()
                kept, overflow = _split_entries(
                    merged, self.settings.changelog_entries_per_department
                )
                text, section = apply_write(text, department, "\n".join(kept), WriteMode.REPLACE)
                if overflow:
                    await vault.append_archive(
                        utcnow().strftime("%Y-%m"), overflow, prefix="changelog"
                    )
            await vault.write(CHANGELOG_FILE, text)
        return line, section.etag if section else None

    async def _agent_event(
        self, project: Project, role: str, agent_id: str, kind: str, summary: str
    ) -> None:
        line, etag = await self._append_changelog(
            project, role, "system", "agent", summary, ["all"]
        )
        await self._commit_mutation(
            project,
            event_type=kind,
            agent_id=agent_id,
            department=role,
            subagent_role=None,
            target_uri=brain_uri(project.project_id, CHANGELOG_FILE, slugify(role)),
            summary=summary,
            importance="normal",
            previous_etag=None,
            new_etag=etag,
            events=[
                Event(
                    kind,
                    project.project_id,
                    file=CHANGELOG_FILE,
                    heading=role,
                    slug=slugify(role),
                    etag=etag,
                    agent_id=agent_id,
                    department=role,
                    summary=summary,
                    affects=["all"],
                )
            ],
            affects=["all"],
        )
        await self._refresh_digest(project)

    async def _refresh_digest(self, project: Project) -> str:
        company_log = self.company_log(project)
        today = utcnow().date()
        blocks: list[str] = []
        for offset in range(DIGEST_DAYS):
            day = date.fromordinal(today.toordinal() - offset)
            entries = [e for e in await company_log.entries_for_day(day) if _in_digest(e)]
            if not entries:
                continue
            blocks.append(_render_digest_day(day, entries))
        body = "\n\n".join(blocks) or "- (no digest yet)"
        vault = self.vault(project)
        async with self.locks.hold(project.project_id, CHANGELOG_FILE, slugify(DIGEST_HEADING)):
            text = await vault.read(CHANGELOG_FILE)
            if parse(text).find(DIGEST_HEADING) is None:
                text, _ = apply_write(text, DIGEST_HEADING, body, WriteMode.CREATE)
            else:
                text, _ = replace_section_tree(text, DIGEST_HEADING, body)
            await vault.write(CHANGELOG_FILE, text)
        return today.isoformat()

    def _affects(self, policy: Policy, affects: list[str] | None, writer: str) -> list[str]:
        if not affects:
            return []
        if len(affects) > 10:
            raise BrainError(400, "INVALID", "affects may list at most 10 departments")
        known = set(policy.roles) | {ADMIN_ROLE, "all"}
        cleaned: list[str] = []
        for item in affects:
            value = str(item).strip().casefold()
            if not value or value == writer:
                continue
            if value not in known:
                raise BrainError(
                    400,
                    "INVALID",
                    f"unknown department {value!r} in affects",
                    departments=sorted(known - {"all"}),
                )
            if value not in cleaned:
                cleaned.append(value)
        return cleaned

    async def _propose(
        self,
        principal: Principal,
        project: Project,
        file_name: str,
        heading: str,
        content: str,
        summary: str,
        mode: WriteMode,
        expected_etag: str | None,
    ) -> WriteResult:
        slug = slugify(_leaf(heading))
        proposal = Proposal(
            proposal_id="pr_" + secrets.token_hex(8),
            project_id=project.project_id,
            key_id=principal.key_id or "master",
            agent_id=principal.agent_id,
            department=principal.role,
            target_uri=brain_uri(project.project_id, file_name, slug),
            mode=mode.value,
            patch_content=content,
            compact_summary=summary,
            expected_etag=expected_etag,
            created_at=utcnow(),
        )
        async with self.db.transaction() as session:
            session.add(proposal)
        entry = await self._commit_mutation(
            project,
            event_type="proposal_created",
            agent_id=principal.agent_id,
            department=principal.role,
            subagent_role=None,
            target_uri=proposal.target_uri,
            summary=f"proposed: {summary}",
            importance="low",
            previous_etag=expected_etag,
            new_etag=None,
            events=[
                Event(
                    "proposal_created",
                    project.project_id,
                    file=file_name,
                    heading=heading,
                    slug=slug,
                    agent_id=principal.agent_id,
                    department=principal.role,
                    summary=summary,
                    importance="low",
                )
            ],
            commit=False,
        )
        return WriteResult(
            "PENDING_APPROVAL",
            file_name,
            heading,
            slug,
            None,
            expected_etag,
            entry.log_id,
            proposal.proposal_id,
            extra={
                "message": "an admin must approve this change in the Approvals panel; "
                "poll get_proposal for the outcome"
            },
        )

    async def _apply(
        self,
        project: Project,
        *,
        agent_id: str,
        department: str,
        subagent_role: str | None,
        file_name: str,
        heading: str,
        content: str,
        mode: WriteMode,
        expected_etag: str | None,
        summary: str,
        importance: str,
        event_type: str,
    ) -> WriteResult:
        vault = self.vault(project)
        parent, leaf = _split_heading(heading)
        slug = slugify(leaf)
        policy = await self.policy(project)
        affects = affected_roles(policy, file_name, slug, exclude=department)
        async with self.locks.hold(project.project_id, file_name, slug):
            text = await vault.read(file_name)
            doc = parse(text)
            section = doc.find(leaf)
            if mode is WriteMode.CREATE:
                if section is not None:
                    raise BrainError(
                        409,
                        "CONFLICT",
                        f"heading {leaf!r} already exists; use replace or append",
                        current_etag=section.etag,
                        current_content=doc.body(section),
                    )
            elif section is None:
                raise BrainError(
                    404,
                    "NOT_FOUND",
                    f"heading {heading!r} not found in {file_name}; use mode=create to add it",
                    available=[s.heading for s in doc.sections],
                )
            if section is not None and expected_etag and expected_etag != section.etag:
                raise BrainError(
                    409,
                    "CONFLICT",
                    "section changed since you read it; merge and retry with the current etag",
                    current_etag=section.etag,
                    current_content=doc.body(section),
                )
            try:
                new_text, new_section = apply_write(
                    text, leaf, content, mode, level=3 if parent else 2, parent=parent
                )
            except SectionNotFound as error:
                raise BrainError(404, "NOT_FOUND", f"parent heading {error} not found") from error
            except SectionExists as error:
                raise BrainError(409, "CONFLICT", f"heading {error} already exists") from error
            await vault.write(file_name, new_text)
            new_doc = parse(new_text)
            previous_etag = section.etag if section else None
            new_etag = new_section.etag if new_section else None
            kind = {
                WriteMode.CREATE: "section_created",
                WriteMode.DELETE: "section_deleted",
            }.get(mode, "section_updated")
            entry = await self._commit_mutation(
                project,
                event_type=event_type,
                agent_id=agent_id,
                department=department,
                subagent_role=subagent_role,
                target_uri=brain_uri(project.project_id, file_name, slug),
                summary=summary,
                importance=importance,
                previous_etag=previous_etag,
                new_etag=new_etag,
                events=[
                    Event(
                        kind,
                        project.project_id,
                        file=file_name,
                        heading=leaf,
                        slug=slug,
                        etag=new_etag,
                        previous_etag=previous_etag,
                        agent_id=agent_id,
                        department=department,
                        subagent_role=subagent_role,
                        summary=summary,
                        importance=importance,
                        affects=affects,
                    )
                ],
                affects=affects,
            )
        if file_name == PERMISSIONS_FILE:
            vault._policy_cache = None
        return WriteResult(
            "ok",
            file_name,
            leaf,
            slug,
            new_etag,
            previous_etag,
            entry.log_id,
            file_etag=new_doc.etag,
            extra={"affects": affects} if affects else {},
        )

    async def _commit_mutation(
        self,
        project: Project,
        *,
        event_type: str,
        agent_id: str,
        department: str,
        subagent_role: str | None,
        target_uri: str,
        summary: str,
        importance: str,
        previous_etag: str | None,
        new_etag: str | None,
        events: list[Event],
        commit: bool = True,
        affects: list[str] | None = None,
    ) -> MutationLog:
        company_log = self.company_log(project)
        entry = company_log.build_entry(
            event_type=event_type,
            agent_id=agent_id,
            department=department,
            subagent_role=subagent_role,
            target_uri=target_uri,
            compact_summary=summary,
            importance=importance,
            previous_etag=previous_etag,
            new_etag=new_etag,
            affects=affects,
        )
        async with self.db.transaction() as session:
            session.add(entry)
            await session.flush()
            actor = entry.actor
            for event in events:
                event.log_id = entry.log_id
                session.add(self.git.build_job(project.project_id, "broadcast", event.to_dict()))
            if commit:
                session.add(
                    self.git.build_job(
                        project.project_id,
                        "git_commit",
                        {
                            "actor": actor,
                            "summary": summary,
                            "target": target_uri,
                            "log_ids": [entry.log_id],
                        },
                        delay=self.settings.git_commit_debounce_seconds,
                    )
                )
        await company_log.rewrite_hot_log()
        self.git.wake()
        return entry

    async def _publish_payload(self, payload: dict[str, Any]) -> None:
        await self.hub.publish(Event.from_json(json.dumps(payload)))

    async def _attach_commit(self, project_id: str, log_ids: list[int], sha: str) -> None:
        await self.company_log(project_id).attach_commit(log_ids, sha)

    async def system_event(self, project_id: str, kind: str, summary: str) -> None:
        try:
            project = await self.get_project(project_id)
        except BrainError:
            return
        await self._commit_mutation(
            project,
            event_type="system",
            agent_id="system",
            department="system",
            subagent_role=None,
            target_uri=brain_uri(project.project_id, kind),
            summary=self._summary(summary),
            importance="low" if kind == "project_created" else "normal",
            previous_etag=None,
            new_etag=None,
            events=[
                Event(
                    "system",
                    project.project_id,
                    summary=summary,
                    agent_id="system",
                    department="system",
                    importance="low",
                )
            ],
            commit=False,
        )

    async def _files_pulled(
        self, project_id: str, changed: list[str], sha: str, source: str = "remote"
    ) -> None:
        project = await self.get_project(project_id)
        vault = self.vault(project)
        events: list[Event] = []
        touched: list[str] = []
        for path in changed:
            try:
                name = validate_file_name(path)
            except FileNotAllowed:
                continue
            doc = parse(await vault.read(name))
            touched.append(name)
            summary = (
                f"pulled {name} from remote" if source == "remote" else f"{name} edited on disk"
            )
            events.append(
                Event(
                    "file_pulled",
                    project_id,
                    file=name,
                    etag=doc.etag,
                    agent_id="github",
                    department="human",
                    summary=summary,
                )
            )
            events.extend(
                Event(
                    "section_updated",
                    project_id,
                    file=name,
                    heading=s.heading,
                    slug=s.slug,
                    etag=s.etag,
                    agent_id="github",
                    department="human",
                    summary=summary,
                )
                for s in doc.sections
            )
        if not touched:
            return
        target = (
            brain_uri(project_id, touched[0])
            if len(touched) == 1
            else brain_uri(project_id, "vault")
        )
        await self._commit_mutation(
            project,
            event_type="human_pull",
            agent_id="github",
            department="human",
            subagent_role=None,
            target_uri=target,
            summary=(
                f"remote commit {sha[:7]} updated "
                if source == "remote"
                else f"disk edit {sha[:7]} updated "
            )
            + ", ".join(touched),
            importance="normal",
            previous_etag=None,
            new_etag=None,
            events=events,
            commit=False,
            affects=["all"],
        )
        vault._policy_cache = None

    async def ingest_github_push(
        self, project: Project, delivery_id: str | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if delivery_id and self._seen(delivery_id):
            return {"ok": True, "duplicate": True}
        repo_name, clone_url, commits = parse_github_push(payload)
        company_log = self.company_log(project)
        logged = 0
        for commit in commits:
            first_line = commit.message.splitlines()[0] if commit.message else "(no message)"
            summary = self._summary(f'{commit.author}: "{first_line}" ({len(commit.files)} files)')
            await company_log.record(
                rewrite=False,
                event_type="human_commit",
                agent_id="github",
                department="human",
                target_uri=f"repo://{repo_name}@{commit.sha[:7]}",
                compact_summary=summary,
                importance="normal",
                commit_sha=commit.sha,
            )
            await self.hub.publish(
                Event(
                    "human_commit",
                    project.project_id,
                    agent_id="github",
                    department="human",
                    summary=summary,
                )
            )
            logged += 1
        if logged:
            await company_log.rewrite_hot_log()
        pull_scheduled = False
        if project.git_remote_url and normalize_remote(project.git_remote_url) == normalize_remote(
            clone_url
        ):
            await self.git.enqueue(
                project.project_id,
                "webhook_pull",
                {"ref": payload.get("ref"), "after": payload.get("after")},
            )
            pull_scheduled = True
        analysis_started = False
        if commits and self.analyzer.auto_trigger(project, clone_url):
            analysis_started = self.analyzer.start(project.project_id, "push")
        return {
            "ok": True,
            "commits_logged": logged,
            "pull_scheduled": pull_scheduled,
            "analysis_started": analysis_started,
        }

    def _seen(self, delivery_id: str) -> bool:
        now = time.monotonic()
        for key, stamp in list(self._deliveries.items()):
            if now - stamp > DELIVERY_TTL_SECONDS:
                self._deliveries.pop(key, None)
        if delivery_id in self._deliveries:
            return True
        self._deliveries[delivery_id] = now
        return False

    async def list_proposals(
        self, project: Project, status: str | None = "pending"
    ) -> list[Proposal]:
        async with self.db.session() as session:
            query = (
                select(Proposal)
                .where(Proposal.project_id == project.project_id)
                .order_by(Proposal.created_at.desc())
            )
            if status:
                query = query.where(Proposal.status == status)
            return list(await session.scalars(query))

    async def get_proposal(
        self, principal: Principal, project: Project, proposal_id: str
    ) -> Proposal:
        async with self.db.session() as session:
            proposal = await session.get(Proposal, proposal_id)
        if proposal is None or proposal.project_id != project.project_id:
            raise BrainError(404, "NOT_FOUND", f"proposal {proposal_id!r} not found")
        if not principal.is_admin and proposal.key_id != principal.key_id:
            raise BrainError(403, "FORBIDDEN", "this proposal belongs to another agent")
        return proposal

    async def decide_proposal(
        self,
        project: Project,
        proposal_id: str,
        approve: bool,
        note: str | None,
        reviewer: str = "admin",
    ) -> Proposal:
        async with self.db.session() as session:
            proposal = await session.get(Proposal, proposal_id)
        if proposal is None or proposal.project_id != project.project_id:
            raise BrainError(404, "NOT_FOUND", f"proposal {proposal_id!r} not found")
        if proposal.status != "pending":
            raise BrainError(409, "CONFLICT", f"proposal already {proposal.status}")
        file_name = proposal.target_uri.split("/", 3)[-1].split("#", 1)[0]
        heading = proposal.target_uri.split("#", 1)[1] if "#" in proposal.target_uri else ""
        outcome = "rejected"
        if approve:
            try:
                await self._apply(
                    project,
                    agent_id=proposal.agent_id,
                    department=proposal.department,
                    subagent_role=None,
                    file_name=file_name,
                    heading=heading,
                    content=proposal.patch_content,
                    mode=WriteMode(proposal.mode),
                    expected_etag=proposal.expected_etag,
                    summary=proposal.compact_summary + f" (approved by {reviewer})",
                    importance="high",
                    event_type="approved_update",
                )
                outcome = "approved"
            except BrainError as error:
                if error.code != "CONFLICT":
                    raise
                outcome = "superseded"
                note = (note or "") + f" [section changed before approval: {error.detail}]"
        async with self.db.transaction() as session:
            row = await session.get(Proposal, proposal_id)
            if row is not None:
                row.status = outcome
                row.reviewer_note = (note or "").strip() or None
                row.decided_at = utcnow()
                proposal = row
        if outcome != "approved":
            note_text = f" — {note.strip()}" if note and note.strip() else ""
            await self._commit_mutation(
                project,
                event_type=f"proposal_{outcome}",
                agent_id=proposal.agent_id,
                department=proposal.department,
                subagent_role=None,
                target_uri=proposal.target_uri,
                summary=self._summary(f"{outcome}: {proposal.compact_summary}{note_text}"),
                importance="low",
                previous_etag=None,
                new_etag=None,
                events=[
                    Event(
                        f"proposal_{outcome}",
                        project.project_id,
                        file=file_name,
                        slug=heading or None,
                        agent_id=proposal.agent_id,
                        department=proposal.department,
                        summary=proposal.compact_summary,
                        importance="low",
                    )
                ],
                commit=False,
            )
        return proposal

    def _annotate(self, role: str, entries: list[MutationLog]) -> list[dict[str, Any]]:
        annotated: list[dict[str, Any]] = []
        for entry in entries:
            data = entry.compact()
            if CompanyLog.affects_role(role, entry):
                data["relevance"] = "affects_you"
                data["hint"] = AFFECTS_HINT
            else:
                data["relevance"] = "watched"
            annotated.append(data)
        return annotated

    async def log_delta(
        self, principal: Principal, project: Project, since: int, limit: int, include_low: bool
    ) -> dict[str, Any]:
        self._limit(principal, "read")
        policy = await self.policy(project)
        role = self.role_of(principal)
        entries, latest = await self.company_log(project).delta(
            max(0, since), max(1, min(limit, 200)), policy, role, include_low
        )
        await self.auth.mark_synced(principal.key_id, latest)
        annotated = self._annotate(role, entries)
        return {
            "entries": annotated,
            "latest_id": latest,
            "count": len(annotated),
            "affects_you": sum(1 for e in annotated if e["relevance"] == "affects_you"),
        }

    async def sync_banner(self, principal: Principal, project: Project) -> dict[str, Any] | None:
        if principal.is_master or principal.key_id is None:
            return None
        company_log = self.company_log(project)
        latest = await company_log.latest_id()
        async with self.db.session() as session:
            key = await session.get(ApiKey, principal.key_id)
        if key is None:
            return None
        since = key.last_sync_log_id
        if latest <= since:
            return None
        policy = await self.policy(project)
        entries, seen = await company_log.delta(
            since, self.settings.inbox_inline_limit + 1, policy, principal.role, False
        )
        if not entries:
            await self.auth.mark_synced(principal.key_id, seen)
            return None
        if len(entries) <= self.settings.inbox_inline_limit and seen >= latest:
            await self.auth.mark_synced(principal.key_id, seen)
            inbox = self._annotate(principal.role, entries)
            affecting = sum(1 for e in inbox if e["relevance"] == "affects_you")
            hint = (
                f"[brain] {len(entries)} change(s) since your last sync, included below; "
                "re-read affected sections if you rely on them."
            )
            if affecting:
                hint += f" {affecting} of them affect your department: adjust and log it."
            return {"unread": len(entries), "latest_log_id": seen, "hint": hint, "inbox": inbox}
        return {
            "unread": max(len(entries), latest - since),
            "latest_log_id": latest,
            "hint": (
                f"[brain] {latest - since} change(s) since your last sync (latest_id {latest}). "
                f"Call get_company_log_delta(since_entry_id={since})."
            ),
        }

    async def admin_log(self, project: Project, since: int, limit: int) -> dict[str, Any]:
        entries, latest = await self.company_log(project).entries(
            max(0, since), max(1, min(limit, 500))
        )
        return {"entries": [e.to_dict() for e in entries], "latest_id": latest}

    async def recent_log(self, project: Project, limit: int) -> list[dict[str, Any]]:
        entries = await self.company_log(project).recent(max(1, min(limit, 500)))
        return [e.to_dict() for e in entries]

    async def diff(self, project: Project, log_id: int) -> dict[str, Any]:
        entry = await self.company_log(project).get(log_id)
        if entry is None:
            raise BrainError(404, "NOT_FOUND", f"log entry {log_id} not found")
        if not entry.commit_sha:
            return {
                "log_id": log_id,
                "commit_sha": None,
                "diff": None,
                "entry": format_entry(entry),
            }
        repo = await self.repo_for(project.project_id)
        if repo is None:
            raise BrainError(404, "NOT_FOUND", "repository not found")
        try:
            diff = await repo.show(entry.commit_sha)
        except Exception as error:
            raise BrainError(
                404, "NOT_FOUND", f"commit {entry.commit_sha} not available: {error}"
            ) from error
        return {
            "log_id": log_id,
            "commit_sha": entry.commit_sha,
            "diff": diff,
            "entry": format_entry(entry),
        }

    async def sync_db_schema(
        self, principal: Principal, project: Project, payload: dict[str, Any], compact_summary: str
    ) -> dict[str, Any]:
        self._limit(principal, "write")
        role = self.role_of(principal)
        policy = await self.policy(project)
        if not policy.write_permission(role, SCHEMA_FILE).writable:
            raise BrainError(403, "FORBIDDEN", f"role {role} may not write {SCHEMA_FILE}")
        tables = payload.get("tables")
        if not isinstance(tables, list) or not tables:
            raise BrainError(400, "INVALID", "schema_payload.tables must be a non-empty list")
        if len(tables) > 300:
            raise BrainError(413, "TOO_LARGE", "schema payload is limited to 300 tables")
        summary = self._summary(compact_summary or "database schema synchronised")
        vault = self.vault(project)
        affects = affected_roles(policy, SCHEMA_FILE, None, exclude=role)
        changed: list[str] = []
        events: list[Event] = []
        async with self.locks.hold(project.project_id, SCHEMA_FILE):
            text = await vault.read(SCHEMA_FILE)
            original = parse(text)
            if original.find(SCHEMA_TABLES_HEADING) is None:
                text, _ = apply_write(text, SCHEMA_TABLES_HEADING, "", WriteMode.CREATE)
            for table in tables:
                if not isinstance(table, dict) or not table.get("name"):
                    continue
                name = str(table["name"]).strip()
                body = _render_table(table)
                doc = parse(text)
                existing = doc.find(name)
                if existing is not None and existing.level == 3:
                    if normalize(doc.body(existing).splitlines()) == normalize(body.splitlines()):
                        continue
                    text, section = apply_write(text, name, body, WriteMode.REPLACE)
                    kind = "section_updated"
                else:
                    text, section = apply_write(
                        text, name, body, WriteMode.CREATE, level=3, parent=SCHEMA_TABLES_HEADING
                    )
                    kind = "section_created"
                changed.append(name)
                events.append(
                    Event(
                        kind,
                        project.project_id,
                        file=SCHEMA_FILE,
                        heading=name,
                        slug=section.slug if section else slugify(name),
                        etag=section.etag if section else None,
                        agent_id=principal.agent_id,
                        department=role,
                        summary=summary,
                        affects=affects,
                    )
                )
            engine = payload.get("engine")
            if isinstance(engine, dict) and engine.get("name"):
                row = (
                    f"| {engine.get('name')} | {engine.get('engine', '')} | "
                    f"{engine.get('version', '')} | {engine.get('purpose', '')} |"
                )
                doc = parse(text)
                engines = doc.find(SCHEMA_ENGINES_HEADING)
                if engines is not None and row not in doc.body(engines):
                    text, _ = apply_write(text, SCHEMA_ENGINES_HEADING, row, WriteMode.APPEND)
            if not changed:
                return {"status": "NOT_MODIFIED", "changed_tables": [], "file_etag": original.etag}
            stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
            line = f"- {stamp} {principal.agent_id}: {summary} ({', '.join(changed)})"
            doc = parse(text)
            if doc.find(SCHEMA_MIGRATIONS_HEADING) is not None:
                migrations = _without_placeholder(doc.body(doc.require(SCHEMA_MIGRATIONS_HEADING)))
                text, _ = apply_write(
                    text,
                    SCHEMA_MIGRATIONS_HEADING,
                    (line + "\n" + migrations).strip(),
                    WriteMode.REPLACE,
                )
            else:
                text, _ = apply_write(text, SCHEMA_MIGRATIONS_HEADING, line, WriteMode.CREATE)
            await vault.write(SCHEMA_FILE, text)
            new_doc = parse(text)
            events.append(
                Event(
                    "schema_synced",
                    project.project_id,
                    file=SCHEMA_FILE,
                    etag=new_doc.etag,
                    agent_id=principal.agent_id,
                    department=role,
                    summary=summary,
                    affects=affects,
                )
            )
            entry = await self._commit_mutation(
                project,
                event_type="db_schema",
                agent_id=principal.agent_id,
                department=role,
                subagent_role=None,
                target_uri=brain_uri(project.project_id, SCHEMA_FILE, "tables"),
                summary=summary,
                importance="normal",
                previous_etag=original.etag,
                new_etag=new_doc.etag,
                events=events,
                affects=affects,
            )
        return {
            "status": "ok",
            "changed_tables": changed,
            "file_etag": new_doc.etag,
            "log_id": entry.log_id,
            "affects": affects,
        }

    async def touch_session(
        self,
        principal: Principal,
        transport: str,
        session_id: str,
        client_info: str | None = None,
    ) -> None:
        if principal.is_master or principal.key_id is None:
            return
        async with self.db.transaction() as session:
            row = await session.get(AgentSession, session_id)
            now = utcnow()
            if row is None:
                session.add(
                    AgentSession(
                        session_id=session_id,
                        project_id=principal.project_id,
                        key_id=principal.key_id,
                        agent_id=principal.agent_id,
                        department_role=principal.role,
                        transport=transport,
                        client_info=client_info,
                        connected_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                row.last_seen_at = now
                row.disconnected_at = None
                if client_info:
                    row.client_info = client_info

    async def close_session(self, session_id: str) -> None:
        async with self.db.transaction() as session:
            row = await session.get(AgentSession, session_id)
            if row is not None:
                row.disconnected_at = utcnow()

    async def live_sessions(self, project_id: str) -> list[AgentSession]:
        cutoff = utcnow().timestamp() - LIVE_SESSION_SECONDS
        async with self.db.session() as session:
            rows = list(
                await session.scalars(
                    select(AgentSession)
                    .where(
                        AgentSession.project_id == project_id,
                        AgentSession.disconnected_at.is_(None),
                    )
                    .order_by(AgentSession.last_seen_at.desc())
                )
            )
        return [r for r in rows if r.last_seen_at and r.last_seen_at.timestamp() >= cutoff]

    async def list_keys(self, project: Project) -> list[ApiKey]:
        async with self.db.session() as session:
            return list(
                await session.scalars(
                    select(ApiKey)
                    .where(ApiKey.project_id == project.project_id)
                    .order_by(ApiKey.created_at.desc())
                )
            )

    async def issue_key(
        self, project: Project, role: str, agent_id: str, label: str | None
    ) -> tuple[ApiKey, str]:
        roles = await self.roles(project)
        if role not in roles:
            raise BrainError(
                400,
                "INVALID",
                f"role {role!r} is not defined in {PERMISSIONS_FILE}; available: {', '.join(roles)}",
            )
        try:
            record, plaintext = await self.auth.issue(project.project_id, role, agent_id, label)
        except AuthError as error:
            raise BrainError(error.status, "INVALID", error.detail) from error
        await self._agent_event(
            project, role, agent_id, "agent_joined", f"New agent {agent_id} joined as {role}"
        )
        return record, plaintext

    async def quick_key(
        self,
        name: str,
        project_id: str | None = None,
        project_name: str | None = None,
        role: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        label = " ".join((name or "").split())
        if not label:
            raise BrainError(
                400, "INVALID", "name is required, for example 'Sales Hermes on VPS 1'"
            )
        project = await self._project_for_quick_key(project_id, project_name, label)
        roles = await self.roles(project)
        chosen = (role or "").strip().casefold() or detect_role(label, roles)
        if chosen not in roles:
            raise BrainError(
                400, "INVALID", f"role {chosen!r} is not defined for this project", roles=roles
            )
        base = slug_identifier(agent_id or label)
        if not is_identifier(base):
            raise BrainError(
                400, "INVALID", "could not derive an agent ID from that name; use a longer one"
            )
        taken = {k.agent_id for k in await self.list_keys(project) if k.active}
        candidate, counter = base, 2
        while candidate in taken:
            candidate = f"{base[:37].rstrip('-')}-{counter:02d}"
            counter += 1
        record, plaintext = await self.issue_key(project, chosen, candidate, label)
        snippets = self.connection_snippets(plaintext)
        return {
            "project": self.project_view(project),
            "key": record.to_dict(self.settings.stale_after_minutes),
            "plaintext": plaintext,
            "role": chosen,
            "agent_id": candidate,
            "mcp_url": f"{self.settings.public_url}/mcp",
            "command": snippets["claude_code"].strip(),
            "snippets": snippets,
        }

    async def quick_project(
        self,
        name: str,
        first_agent: str | None = None,
        repo_url: str | None = None,
        backup: bool = False,
    ) -> dict[str, Any]:
        label = " ".join((name or "").split())
        base = slug_identifier(label)
        if not label or not is_identifier(base):
            raise BrainError(
                400, "INVALID", "enter a company or project name of at least three characters"
            )
        candidate, counter = base, 2
        while await self.project_exists(candidate):
            candidate = f"{base[:37].rstrip('-')}-{counter:02d}"
            counter += 1
        project = await self.create_project(candidate, label)
        result = await self.quick_key(first_agent or "Primary agent", candidate)
        result["github"] = None
        result["github_error"] = None
        if repo_url and repo_url.strip():
            try:
                project, secret = await self.connect_github(project, repo_url.strip())
                result["github"] = {"repo_url": repo_url.strip(), "webhook_secret": secret}
            except BrainError as error:
                result["github_error"] = error.detail
        result["backup"] = None
        result["backup_error"] = None
        if backup:
            try:
                result["backup"] = await self.create_backup_repo(project)
            except BrainError as error:
                result["backup_error"] = error.detail
        result["project"] = await self.project_overview(await self.get_project(candidate))
        return result

    async def connect_github(
        self,
        project: Project,
        repo_url: str,
        token: str | None = None,
        branch: str | None = None,
        analyze_on_push: bool | None = None,
        clear_token: bool = False,
    ) -> tuple[Project, str | None]:
        from .repo_analysis import github_settings, parse_repo_url

        try:
            parse_repo_url(repo_url)
        except Exception as error:
            raise BrainError(400, "INVALID", str(getattr(error, "detail", error))) from error
        current = github_settings(project)
        if clear_token:
            stored_token = ""
        elif token and token.strip():
            stored_token = token.strip()
        else:
            stored_token = current.get("token", "")
        data = {
            **current,
            "repo_url": repo_url.strip(),
            "branch": branch.strip() if branch is not None else current.get("branch", ""),
            "token": stored_token,
            "analyze_on_push": (
                analyze_on_push
                if analyze_on_push is not None
                else current.get("analyze_on_push", True)
            ),
            "connected_at": current.get("connected_at") or iso(utcnow()),
        }
        fields: dict[str, Any] = {"settings": {"github": data}}
        generated_secret = None
        if not project.webhook_secret:
            generated_secret = secrets.token_urlsafe(32)
            fields["webhook_secret"] = generated_secret
        updated = await self.update_project(project.project_id, **fields)
        if self.analyzer.auto_trigger(updated, data["repo_url"]):
            self.analyzer.start(updated.project_id, "connect")
        return updated, generated_secret

    async def create_backup_repo(self, project: Project) -> dict[str, Any]:
        token = self.settings.github_token
        if not token:
            raise BrainError(
                409,
                "NO_GITHUB_ACCOUNT",
                "connect your GitHub account first (a token with repo scope)",
            )
        name = backup_repo_name(project.project_id)
        try:
            repo = await create_repo(
                token,
                name,
                f"AI Computer Brain backup of {project.project_name}: the shared memory files of its agents",
            )
        except GitHubError as error:
            raise BrainError(
                error.status if error.status in (401, 403) else 502, "GITHUB", error.detail
            ) from error
        backup = {
            "repo": repo["full_name"],
            "html_url": repo["html_url"],
            "private": repo["private"],
            "created_at": iso(utcnow()),
            "reused": not repo["created"],
        }
        await self.update_project(
            project.project_id,
            git_remote_url=push_url(token, repo["full_name"]),
            settings={"backup": backup},
        )
        if self.settings.git_push_enabled:
            await self.git.enqueue(project.project_id, "git_push", {"branch": project.git_branch})
        await self.system_event(
            project.project_id,
            "backup_repo",
            f"Backup repository {repo['full_name']} connected; every change is pushed there",
        )
        return backup

    async def sync_disk_changes(self) -> list[str]:
        synced: list[str] = []
        for project in await self.list_projects():
            repo = self.git_repo(project.project_id, self.vault(project).root)
            if not await repo.is_repo() or await self.git._has_open(
                project.project_id, "git_commit"
            ):
                continue
            try:
                status = await repo.run("status", "--porcelain")
            except GitError:
                continue
            changed = [line[3:].strip().strip('"') for line in status.splitlines() if line.strip()]
            if not changed:
                continue
            edited = [
                path
                for path in changed
                if path in CANONICAL_FILES and path not in (LOG_FILE, CHANGELOG_FILE)
            ]
            message = (
                "[human/disk] files edited on disk" if edited else "[brain] server-written updates"
            )
            try:
                sha = await repo.commit_all(message)
            except GitError:
                continue
            if not sha:
                continue
            if edited:
                await self._files_pulled(project.project_id, edited, sha, source="disk")
                synced.append(project.project_id)
            if self.settings.git_push_enabled and project.git_remote_url:
                await self.git.enqueue(
                    project.project_id, "git_push", {"branch": project.git_branch}
                )
        return synced

    async def _watch_disk(self) -> None:
        while True:
            await asyncio.sleep(self.settings.disk_watch_seconds)
            try:
                await self.sync_disk_changes()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("disk watch failed")

    async def _project_for_quick_key(
        self, project_id: str | None, project_name: str | None, label: str
    ) -> Project:
        if project_id:
            return await self.get_project(project_id)
        if project_name and project_name.strip():
            candidate = slug_identifier(project_name)
            if not is_identifier(candidate):
                raise BrainError(400, "INVALID", "could not derive a project ID from that name")
            if await self.project_exists(candidate):
                return await self.get_project(candidate)
            return await self.create_project(candidate, project_name.strip())
        projects = await self.list_projects()
        if len(projects) == 1:
            return projects[0]
        if not projects:
            return await self.create_project(slug_identifier(label), label)
        raise BrainError(
            400,
            "INVALID",
            "several projects exist; pass project_id",
            projects=[p.project_id for p in projects],
        )

    async def revoke_key(self, project: Project, key_id: str) -> ApiKey:
        record = await self.auth.revoke(project.project_id, key_id)
        if record is None:
            raise BrainError(404, "NOT_FOUND", f"key {key_id!r} not found")
        await self._agent_event(
            project,
            record.department_role,
            record.agent_id,
            "agent_revoked",
            f"Agent {record.agent_id} ({record.department_role}) was revoked",
        )
        return record

    async def find_key(self, project: Project, key_id: str) -> ApiKey:
        for record in await self.list_keys(project):
            if record.key_id == key_id:
                return record
        raise BrainError(404, "NOT_FOUND", f"key {key_id!r} not found")

    def connect_info(self, project: Project, record: ApiKey) -> dict[str, Any]:
        base = self.settings.public_url
        plaintext = self.auth.reveal(record) if record.active else None
        snippets = self.connection_snippets(plaintext or KEY_PLACEHOLDER)
        return {
            "project_id": project.project_id,
            "key": record.to_dict(self.settings.stale_after_minutes),
            "agent_id": record.agent_id,
            "role": record.department_role,
            "mcp_url": f"{base}/mcp",
            "events_url": f"{base}/events",
            "placeholder": KEY_PLACEHOLDER,
            "plaintext": plaintext,
            "command": snippets["claude_code"].strip(),
            "snippets": snippets,
        }

    async def reissue_key(self, project: Project, key_id: str) -> dict[str, Any]:
        old = await self.find_key(project, key_id)
        if old.active:
            await self.auth.revoke(project.project_id, key_id)
        try:
            record, plaintext = await self.auth.issue(
                project.project_id, old.department_role, old.agent_id, old.label
            )
        except AuthError as error:
            raise BrainError(error.status, "INVALID", error.detail) from error
        await self._agent_event(
            project,
            old.department_role,
            old.agent_id,
            "agent_joined",
            f"Agent {old.agent_id} ({old.department_role}) received a replacement key",
        )
        snippets = self.connection_snippets(plaintext)
        return {
            "project": self.project_view(project),
            "key": record.to_dict(self.settings.stale_after_minutes),
            "plaintext": plaintext,
            "role": old.department_role,
            "agent_id": old.agent_id,
            "mcp_url": f"{self.settings.public_url}/mcp",
            "command": snippets["claude_code"].strip(),
            "snippets": snippets,
            "replaced_key_id": key_id,
        }

    async def reseal_keys(self, new_master_key: str) -> int:
        return await self.auth.reseal(new_master_key)

    def connection_snippets(self, plaintext: str) -> dict[str, str]:
        base = self.settings.public_url
        mcp_url = f"{base}/mcp"
        mcp_json = {
            "mcpServers": {
                "company-brain": {
                    "url": mcp_url,
                    "headers": {"Authorization": f"Bearer {plaintext}"},
                }
            }
        }
        return {
            "hermes": (
                "mcp_servers:\n  company-brain:\n"
                f'    url: {mcp_url}\n    headers:\n      Authorization: "Bearer {plaintext}"\n'
            ),
            "openclaw": (
                f"mcporter config add company-brain --url {mcp_url} "
                f'--header "Authorization: Bearer {plaintext}"\n'
            ),
            "claude_code": (
                f"claude mcp add --transport http company-brain {mcp_url} "
                f'--header "Authorization: Bearer {plaintext}"\n'
            ),
            "mcp_json": json.dumps(mcp_json, indent=2) + "\n",
            "python": (
                "from mcp import ClientSession\n"
                "from mcp.client.streamable_http import create_mcp_http_client, "
                "streamable_http_client\n\n"
                f'http = create_mcp_http_client(headers={{"Authorization": "Bearer {plaintext}"}})\n'
                f'async with streamable_http_client("{mcp_url}", http_client=http) as (read, write):\n'
                "    async with ClientSession(read, write) as session:\n"
                "        await session.initialize()\n"
                '        print(await session.call_tool("whoami", {}))\n'
            ),
            "events_curl": f'curl -N -H "Authorization: Bearer {plaintext}" {base}/events\n',
        }

    def _file(self, file_name: str, allow_archive: bool = False) -> str:
        try:
            return validate_file_name(file_name, allow_archive=allow_archive)
        except FileNotAllowed as error:
            raise BrainError(400, "INVALID", str(error), files=list(CANONICAL_FILES)) from error

    def _mode(self, mode: str) -> WriteMode:
        try:
            return WriteMode((mode or "replace").strip().casefold())
        except ValueError as error:
            raise BrainError(
                400, "INVALID", "mode must be replace, append, create or delete"
            ) from error

    def _importance(self, importance: str) -> str:
        value = (importance or "normal").strip().casefold()
        if value not in IMPORTANCE_LEVELS:
            raise BrainError(400, "INVALID", "importance must be low, normal or high")
        return value

    def _summary(self, summary: str) -> str:
        cleaned = clamp_summary(summary or "", self.settings.max_summary_chars)
        if len(cleaned) < 3:
            raise BrainError(
                400, "INVALID", "compact_summary is required: one line other agents can act on"
            )
        return cleaned

    def _check_content(self, content: str, mode: WriteMode) -> None:
        if mode is WriteMode.DELETE:
            return
        if not content or not content.strip():
            raise BrainError(400, "INVALID", "patch_content must not be empty")
        if len(content.encode("utf-8")) > self.settings.max_patch_bytes:
            raise BrainError(
                413, "TOO_LARGE", f"patch_content exceeds {self.settings.max_patch_bytes} bytes"
            )
        secret = find_secret(content)
        if secret:
            raise BrainError(
                422,
                "SECRET_DETECTED",
                f"patch looks like it contains a {secret}; secrets never belong in the brain",
            )


ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("admin", ("admin", "owner", "founder")),
    ("sales", ("sales", "sdr", "crm", "deal", "deals", "revenue")),
    ("marketing", ("marketing", "content", "social", "seo", "growth", "brand", "copy")),
    (
        "engineering",
        (
            "engineering",
            "engineer",
            "developer",
            "dev",
            "code",
            "coding",
            "backend",
            "frontend",
            "claude",
            "cursor",
            "serena",
            "copilot",
        ),
    ),
    ("ops", ("ops", "operations", "support", "openclaw", "assistant", "helpdesk")),
    ("design", ("design", "designer", "ui", "ux", "figma")),
)


def slug_identifier(text: str) -> str:
    slug = NORMALIZE_RE.sub("-", (text or "").casefold()).strip("-")[:40].rstrip("-")
    if len(slug) < 3:
        slug = f"{slug}-agent".strip("-") if slug else "agent-01"
    return slug


def detect_role(name: str, roles: list[str]) -> str:
    words = set(NORMALIZE_RE.sub(" ", name.casefold()).split())
    for role, hints in ROLE_HINTS:
        if role in roles and (role in words or words & set(hints)):
            return role
    return ADMIN_ROLE if ADMIN_ROLE in roles else roles[0]


def _split_heading(heading: str) -> tuple[str | None, str]:
    cleaned = heading.strip().lstrip("#").strip()
    if ">" in cleaned:
        parent, leaf = cleaned.rsplit(">", 1)
        return parent.strip() or None, leaf.strip()
    return None, cleaned


def _leaf(heading: str) -> str:
    return _split_heading(heading)[1]


def _guide_applies(target: str, writable: set[str]) -> bool:
    lowered = target.casefold()
    if "sync_db_schema" in lowered:
        return SCHEMA_FILE in writable
    if "log_agent_activity" in lowered:
        return True
    file_name = target.split("#", 1)[0].split(",")[0].strip().strip("`")
    canonical = {n.upper() for n in CANONICAL_FILES}
    return (
        any(file_name.startswith(name) for name in writable) or file_name.upper() not in canonical
    )


def _changed_sections(old: Document, new: Document):
    previous = {s.slug: s.etag for s in old.sections}
    return [s for s in new.sections if previous.get(s.slug) != s.etag]


def _without_placeholder(body: str) -> str:
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith(PLACEHOLDER_PREFIX)
    ).strip("\n")


def _split_entries(lines: list[str], limit: int) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    overflow: list[str] = []
    count = 0
    for line in lines:
        if line.startswith("- "):
            count += 1
        if count == 0 and not line.strip():
            continue
        (kept if count <= limit else overflow).append(line)
    return kept, overflow


def _normalize_text(text: str) -> str:
    return NORMALIZE_RE.sub(" ", text.casefold()).strip()


def _strip_kind(summary: str) -> str:
    if summary.startswith("[") and "] " in summary:
        return summary.split("] ", 1)[1]
    return summary


def _in_digest(entry: MutationLog) -> bool:
    return entry.event_type in DIGEST_EVENT_LABELS


def _render_digest_day(day: date, entries: list[MutationLog]) -> str:
    counts: dict[str, int] = {}
    departments: list[str] = []
    lines: list[str] = []
    for entry in entries:
        label = DIGEST_EVENT_LABELS.get(entry.event_type, "other")
        counts[label] = counts.get(label, 0) + 1
        if entry.department not in departments:
            departments.append(entry.department)
        stamp = (entry.created_at or datetime.min).strftime("%H:%M")
        summary = entry.compact_summary
        if label == "update":
            target = entry.target_uri.split("/", 3)[-1]
            summary = f"{target} — {summary}"
        line = f"- {stamp} {entry.actor} [{label}] {summary}"
        if entry.affects_list:
            line += " → affects: " + ", ".join(entry.affects_list)
        lines.append(line)
    totals = ", ".join(f"{n} {label}{'s' if n != 1 else ''}" for label, n in sorted(counts.items()))
    header = (
        f"### {day.isoformat()}\n\nSummary: {totals} — departments: {', '.join(departments)}.\n"
    )
    return header + "\n" + "\n".join(lines)


def _render_table(table: dict[str, Any]) -> str:
    rows = ["| Column | Type | Nullable | Default | Notes |", "|---|---|---|---|---|"]
    columns = table.get("columns") if isinstance(table.get("columns"), list) else []
    for column in columns:
        if not isinstance(column, dict):
            continue
        nullable = column.get("nullable")
        nullable_text = "" if nullable is None else ("yes" if nullable else "no")
        default = column.get("default")
        rows.append(
            "| {} | {} | {} | {} | {} |".format(
                column.get("name", ""),
                column.get("type", ""),
                nullable_text,
                "" if default in (None, "") else default,
                column.get("notes", "") or "",
            )
        )
    indexes = table.get("indexes") if isinstance(table.get("indexes"), list) else []
    if indexes:
        rows.append("")
        rows.append("Indexes: " + ", ".join(str(i) for i in indexes))
    if table.get("notes"):
        rows.append("")
        rows.append(str(table["notes"]))
    return "\n".join(rows)
