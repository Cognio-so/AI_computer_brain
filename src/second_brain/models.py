from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    git_remote_url: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str] = mapped_column(Text, nullable=False, default="main")
    webhook_secret: Mapped[str | None] = mapped_column(Text)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )

    @property
    def settings(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.settings_json or "{}")
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @settings.setter
    def settings(self, value: dict[str, Any]) -> None:
        self.settings_json = json.dumps(value, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "storage_path": self.storage_path,
            "git_remote_url": self.git_remote_url,
            "git_branch": self.git_branch,
            "webhook_configured": bool(self.webhook_secret),
            "settings": self.settings,
            "created_at": iso(self.created_at),
        }


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    department_role: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    compound_key_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    secret_sealed: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_log_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def is_stale(self, after_minutes: int, now: datetime | None = None) -> bool:
        reference = self.last_sync_at or self.last_used_at
        if reference is None:
            return False
        current = now or utcnow()
        return (current - reference).total_seconds() > after_minutes * 60

    def to_dict(self, stale_after_minutes: int = 30) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "project_id": self.project_id,
            "department_role": self.department_role,
            "agent_id": self.agent_id,
            "label": self.label,
            "key_prefix": self.key_prefix,
            "created_at": iso(self.created_at),
            "last_used_at": iso(self.last_used_at),
            "last_sync_at": iso(self.last_sync_at),
            "last_sync_log_id": self.last_sync_log_id,
            "revoked_at": iso(self.revoked_at),
            "active": self.active,
            "stale": self.active and self.is_stale(stale_after_minutes),
        }


class MutationLog(Base):
    __tablename__ = "mutation_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str] = mapped_column(Text, nullable=False)
    subagent_role: Mapped[str | None] = mapped_column(Text)
    target_uri: Mapped[str] = mapped_column(Text, nullable=False)
    compact_summary: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[str] = mapped_column(Text, nullable=False, default="normal")
    previous_etag: Mapped[str | None] = mapped_column(Text)
    new_etag: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    affects: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )

    @property
    def affects_list(self) -> list[str]:
        return [item for item in (self.affects or "").split(",") if item]

    @property
    def actor(self) -> str:
        base = f"{self.department}/{self.agent_id}"
        return f"{base}[{self.subagent_role}]" if self.subagent_role else base

    @property
    def file_name(self) -> str:
        return target_file(self.target_uri)

    @property
    def slug(self) -> str | None:
        return target_slug(self.target_uri)

    def compact(self) -> dict[str, Any]:
        return {
            "id": self.log_id,
            "ts": iso(self.created_at),
            "type": self.event_type,
            "actor": self.actor,
            "target": self.target_uri,
            "summary": self.compact_summary,
            "importance": self.importance,
            "etag": self.new_etag,
            "affects": self.affects_list,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.compact(),
            "log_id": self.log_id,
            "project_id": self.project_id,
            "agent_id": self.agent_id,
            "department": self.department,
            "subagent_role": self.subagent_role,
            "previous_etag": self.previous_etag,
            "new_etag": self.new_etag,
            "commit_sha": self.commit_sha,
            "created_at": iso(self.created_at),
        }


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    key_id: Mapped[str] = mapped_column(
        String, ForeignKey("api_keys.key_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    department_role: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    client_info: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "key_id": self.key_id,
            "agent_id": self.agent_id,
            "department_role": self.department_role,
            "transport": self.transport,
            "client_info": self.client_info,
            "connected_at": iso(self.connected_at),
            "last_seen_at": iso(self.last_seen_at),
            "disconnected_at": iso(self.disconnected_at),
        }


class Proposal(Base):
    __tablename__ = "proposals"

    proposal_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    key_id: Mapped[str] = mapped_column(
        String, ForeignKey("api_keys.key_id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str] = mapped_column(Text, nullable=False)
    target_uri: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    patch_content: Mapped[str] = mapped_column(Text, nullable=False)
    compact_summary: Mapped[str] = mapped_column(Text, nullable=False)
    expected_etag: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "key_id": self.key_id,
            "agent_id": self.agent_id,
            "department": self.department,
            "target_uri": self.target_uri,
            "mode": self.mode,
            "patch_content": self.patch_content,
            "compact_summary": self.compact_summary,
            "expected_etag": self.expected_etag,
            "status": self.status,
            "reviewer_note": self.reviewer_note,
            "created_at": iso(self.created_at),
            "decided_at": iso(self.decided_at),
        }


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    access_json: Mapped[str] = mapped_column(Text, nullable=False, default='["*"]')
    summary: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str] = mapped_column(Text, nullable=False, default="admin")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )

    @property
    def access(self) -> list[str]:
        try:
            loaded = json.loads(self.access_json or "[]")
        except json.JSONDecodeError:
            return ["*"]
        return [str(v) for v in loaded] if isinstance(loaded, list) and loaded else ["*"]

    @property
    def meta(self) -> dict[str, Any]:
        raw = self.summary or ""
        if raw.startswith("{"):
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                return loaded
        return {"summary": raw} if raw else {}

    @property
    def summary_text(self) -> str:
        return str(self.meta.get("summary") or "")

    @property
    def topics(self) -> list[str]:
        values = self.meta.get("topics")
        return [str(v) for v in values] if isinstance(values, list) else []

    @property
    def departments(self) -> list[str]:
        values = self.meta.get("departments")
        return [str(v) for v in values] if isinstance(values, list) else []

    def allows(self, agent_id: str, role: str) -> bool:
        access = self.access
        return "*" in access or agent_id in access or f"role:{role}" in access

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "project_id": self.project_id,
            "title": self.title,
            "file_name": self.file_name,
            "original_name": self.original_name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "text_chars": self.text_chars,
            "etag": self.content_hash[:8],
            "access": self.access,
            "shared_with_all": "*" in self.access,
            "summary": self.summary_text,
            "topics": self.topics,
            "departments": self.departments,
            "uploaded_by": self.uploaded_by,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def brief(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "file": self.file_name,
            "chars": self.text_chars,
            "summary": self.summary_text[:300],
            "topics": self.topics[:8],
            "departments": self.departments,
            "updated_at": iso(self.updated_at),
        }


class PendingJob(Base):
    __tablename__ = "pending_jobs"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    @property
    def payload(self) -> dict[str, Any]:
        loaded = json.loads(self.payload_json)
        return loaded if isinstance(loaded, dict) else {}


def brain_uri(project_id: str, file_name: str, slug: str | None = None) -> str:
    uri = f"brain://{project_id}/{file_name}"
    return f"{uri}#{slug}" if slug else uri


def target_file(uri: str) -> str:
    path = uri.split("://", 1)[-1]
    path = path.split("#", 1)[0]
    return path.split("/", 1)[-1] if "/" in path else path


def target_slug(uri: str) -> str | None:
    return uri.split("#", 1)[1] if "#" in uri else None
