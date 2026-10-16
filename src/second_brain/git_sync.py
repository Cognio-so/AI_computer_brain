from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .database import Database
from .models import PendingJob, Project, utcnow

log = logging.getLogger("brain.git")

MAX_PUSH_ATTEMPTS = 8
TICK_SECONDS = 0.5
CREDENTIAL_RE = re.compile(r"://([^/@:]+)(:[^/@]*)?@")


class GitError(RuntimeError):
    pass


def mask_remote(url: str | None) -> str | None:
    if not url:
        return url
    return CREDENTIAL_RE.sub(r"://\1:***@", url)


def normalize_remote(url: str | None) -> str:
    if not url:
        return ""
    cleaned = CREDENTIAL_RE.sub("://", url.strip()).rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if cleaned.startswith("git@github.com:"):
        cleaned = "https://github.com/" + cleaned[len("git@github.com:") :]
    return cleaned.casefold()


class GitRepo:
    def __init__(
        self, path: Path, author_name: str, author_email: str, git_dir: Path | None = None
    ) -> None:
        self.path = path
        self.git_dir = git_dir or path / ".git"
        self._env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

    async def run(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.path}",
            *args,
            cwd=str(self.path),
            env={**self._env, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode != 0:
            raise GitError(
                stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed"
            )
        return stdout.decode("utf-8", "replace")

    async def is_repo(self) -> bool:
        return (self.git_dir / "HEAD").is_file()

    def adopt_legacy_repo(self) -> bool:
        legacy = self.path / ".git"
        if legacy == self.git_dir or not legacy.is_dir() or (self.git_dir / "HEAD").is_file():
            return False
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(self.git_dir))
        return True

    async def init(self, branch: str = "main") -> None:
        self.adopt_legacy_repo()
        if await self.is_repo():
            return
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        await self.run("init", "-q", "-b", branch)
        await self.run("config", "user.name", self._env["GIT_AUTHOR_NAME"])
        await self.run("config", "user.email", self._env["GIT_AUTHOR_EMAIL"])

    async def has_changes(self) -> bool:
        status = await self.run("status", "--porcelain")
        return bool(status.strip())

    async def commit_all(self, message: str, author: str | None = None) -> str | None:
        await self.run("add", "-A")
        if not await self.has_changes():
            return None
        args = ["commit", "-q", "-m", message]
        if author:
            args.extend(["--author", author])
        await self.run(*args)
        return await self.head()

    async def head(self) -> str | None:
        with suppress(GitError):
            return (await self.run("rev-parse", "HEAD")).strip()
        return None

    async def current_branch(self) -> str:
        return (await self.run("rev-parse", "--abbrev-ref", "HEAD")).strip()

    async def set_remote(self, url: str | None) -> None:
        remotes = (await self.run("remote")).split()
        if "origin" in remotes:
            await self.run("remote", "remove", "origin")
        if url:
            await self.run("remote", "add", "origin", url)

    async def push(self, branch: str, force: bool = False) -> None:
        args = ["push", "-q", "-u", "origin", f"HEAD:{branch}"]
        if force:
            args.insert(2, "--force")
        await self.run(*args)

    async def pull_rebase(self, branch: str) -> bool:
        await self.run("fetch", "-q", "origin", branch)
        try:
            await self.run("rebase", "-q", f"origin/{branch}")
            return True
        except GitError:
            await self.run("rebase", "--abort", check=False)
            return False

    async def changed_files(self, old: str, new: str) -> list[str]:
        output = await self.run("diff", "--name-only", old, new)
        return [line.strip() for line in output.splitlines() if line.strip()]

    async def show(self, sha: str) -> str:
        return await self.run("show", "--no-color", "--stat", "--patch", sha)

    async def recent_commits(self, limit: int = 20) -> list[dict[str, str]]:
        output = await self.run("log", f"-{limit}", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s")
        commits: list[dict[str, str]] = []
        for line in output.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append(
                    {"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
                )
        return commits


@dataclass(slots=True, frozen=True)
class CommitInfo:
    sha: str
    author: str
    message: str
    files: tuple[str, ...]
    url: str | None


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256=") :])


def parse_github_push(payload: dict[str, object]) -> tuple[str, str, list[CommitInfo]]:
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    full_name = str(repository.get("full_name") or repository.get("name") or "repository")
    clone_url = str(repository.get("clone_url") or repository.get("html_url") or "")
    commits: list[CommitInfo] = []
    raw_commits = payload.get("commits")
    if isinstance(raw_commits, list):
        for raw in raw_commits:
            if not isinstance(raw, dict):
                continue
            author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
            files: list[str] = []
            for key in ("added", "modified", "removed"):
                values = raw.get(key)
                if isinstance(values, list):
                    files.extend(str(v) for v in values)
            commits.append(
                CommitInfo(
                    sha=str(raw.get("id") or raw.get("sha") or ""),
                    author=str(author.get("name") or author.get("username") or "unknown"),
                    message=str(raw.get("message") or "").strip(),
                    files=tuple(files),
                    url=str(raw.get("url")) if raw.get("url") else None,
                )
            )
    return full_name, clone_url, commits


FilesChanged = Callable[[str, list[str], str], Awaitable[None]]
BroadcastHandler = Callable[[dict[str, object]], Awaitable[None]]
SystemLogger = Callable[[str, str, str], Awaitable[None]]


class GitWorker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        repo_for: Callable[[str], Awaitable[GitRepo | None]],
        on_files_changed: FilesChanged,
        on_broadcast: BroadcastHandler,
        on_system_event: SystemLogger,
        attach_commit: Callable[[str, list[int], str], Awaitable[None]],
    ) -> None:
        self._db = db
        self._settings = settings
        self._repo_for = repo_for
        self._on_files_changed = on_files_changed
        self._on_broadcast = on_broadcast
        self._on_system_event = on_system_event
        self._attach_commit = attach_commit
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="brain-git-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)
            self._task = None

    def build_job(
        self, project_id: str, job_type: str, payload: dict[str, object], delay: float = 0
    ) -> PendingJob:
        return PendingJob(
            project_id=project_id,
            job_type=job_type,
            payload_json=json.dumps(payload, separators=(",", ":"), default=str),
            next_attempt_at=utcnow() if delay <= 0 else _later(delay),
        )

    def wake(self) -> None:
        self._wake.set()

    async def enqueue(
        self, project_id: str, job_type: str, payload: dict[str, object], delay: float = 0
    ) -> None:
        job = self.build_job(project_id, job_type, payload, delay)
        async with self._db.transaction() as session:
            session.add(job)
        self._wake.set()

    async def drain(self, max_wait: float = 10) -> None:
        deadline = asyncio.get_running_loop().time() + max_wait
        while asyncio.get_running_loop().time() < deadline:
            if not await self._open_jobs(include_future=True):
                return
            self._wake.set()
            await asyncio.sleep(0.1)

    async def _open_jobs(self, include_future: bool = False) -> list[PendingJob]:
        async with self._db.session() as session:
            query = (
                select(PendingJob).where(PendingJob.done_at.is_(None)).order_by(PendingJob.job_id)
            )
            if not include_future:
                query = query.where(PendingJob.next_attempt_at <= utcnow())
            return list(await session.scalars(query))

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._process_once()
            except Exception:
                log.exception("git worker iteration failed")
            self._wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)

    async def _process_once(self) -> None:
        jobs = await self._open_jobs()
        if not jobs:
            return
        by_project: dict[str, list[PendingJob]] = {}
        for job in jobs:
            by_project.setdefault(job.project_id, []).append(job)
        for project_id, project_jobs in by_project.items():
            broadcasts = [j for j in project_jobs if j.job_type == "broadcast"]
            for job in broadcasts:
                await self._run_broadcast(job)
            commits = [j for j in project_jobs if j.job_type == "git_commit"]
            if commits:
                await self._run_commits(project_id, commits)
            for job in project_jobs:
                if job.job_type == "git_push":
                    await self._run_push(project_id, job)
                elif job.job_type == "webhook_pull":
                    await self._run_pull(project_id, job)

    async def _run_broadcast(self, job: PendingJob) -> None:
        try:
            await self._on_broadcast(job.payload)
            await self._finish(job)
        except Exception as error:
            await self._fail(job, str(error), retry_in=5)

    async def _run_commits(self, project_id: str, jobs: list[PendingJob]) -> None:
        repo = await self._repo_for(project_id)
        if repo is None:
            for job in jobs:
                await self._finish(job, error="project repository not found")
            return
        payloads = [job.payload for job in jobs]
        message = _commit_message(payloads)
        author = _author_from(payloads)
        try:
            await repo.init()
            sha = await repo.commit_all(message, author=author)
        except GitError as error:
            for job in jobs:
                await self._fail(job, str(error), retry_in=10)
            return
        log_ids = [int(i) for p in payloads for i in p.get("log_ids", []) if str(i).isdigit()]
        if sha:
            await self._attach_commit(project_id, log_ids, sha)
        for job in jobs:
            await self._finish(job)
        if sha and self._settings.git_push_enabled:
            project = await self._project(project_id)
            if (
                project
                and project.git_remote_url
                and not await self._has_open(project_id, "git_push")
            ):
                await self.enqueue(project_id, "git_push", {"branch": project.git_branch})

    async def _run_push(self, project_id: str, job: PendingJob) -> None:
        repo = await self._repo_for(project_id)
        project = await self._project(project_id)
        if repo is None or project is None or not project.git_remote_url:
            await self._finish(job, error="no remote configured")
            return
        try:
            await repo.set_remote(project.git_remote_url)
            await repo.push(
                str(job.payload.get("branch") or project.git_branch),
                force=bool(job.payload.get("force")),
            )
            await self._finish(job)
        except GitError as error:
            if job.attempts + 1 >= MAX_PUSH_ATTEMPTS:
                await self._finish(job, error=str(error))
                await self._on_system_event(
                    project_id,
                    "git_push_failed",
                    f"git push failed after {MAX_PUSH_ATTEMPTS} attempts: {mask_remote(str(error))}",
                )
            else:
                await self._fail(job, str(error), retry_in=min(300, 5 * 2 ** (job.attempts + 1)))

    async def _run_pull(self, project_id: str, job: PendingJob) -> None:
        repo = await self._repo_for(project_id)
        project = await self._project(project_id)
        if repo is None or project is None or not project.git_remote_url:
            await self._finish(job, error="no remote configured")
            return
        try:
            before = await repo.head()
            await repo.set_remote(project.git_remote_url)
            clean = await repo.pull_rebase(project.git_branch)
            if not clean:
                await self._on_system_event(
                    project_id,
                    "webhook_conflict",
                    "remote changes conflicted with the vault; server copy kept and pushed",
                )
                await self.enqueue(
                    project_id, "git_push", {"branch": project.git_branch, "force": True}
                )
                await self._finish(job)
                return
            after = await repo.head()
            if before and after and before != after:
                changed = await repo.changed_files(before, after)
                await self._on_files_changed(project_id, changed, after)
            await self._finish(job)
        except GitError as error:
            if job.attempts + 1 >= 3:
                await self._finish(job, error=str(error))
                await self._on_system_event(
                    project_id,
                    "webhook_pull_failed",
                    f"could not pull remote changes: {mask_remote(str(error))}",
                )
            else:
                await self._fail(job, str(error), retry_in=15)

    async def _has_open(self, project_id: str, job_type: str) -> bool:
        async with self._db.session() as session:
            row = await session.scalar(
                select(PendingJob.job_id).where(
                    PendingJob.project_id == project_id,
                    PendingJob.job_type == job_type,
                    PendingJob.done_at.is_(None),
                )
            )
            return row is not None

    async def _project(self, project_id: str) -> Project | None:
        async with self._db.session() as session:
            return await session.get(Project, project_id)

    async def _finish(self, job: PendingJob, error: str | None = None) -> None:
        async with self._db.transaction() as session:
            row = await session.get(PendingJob, job.job_id)
            if row is not None:
                row.done_at = utcnow()
                row.attempts += 1
                row.last_error = error

    async def _fail(self, job: PendingJob, error: str, retry_in: float) -> None:
        async with self._db.transaction() as session:
            row = await session.get(PendingJob, job.job_id)
            if row is not None:
                row.attempts += 1
                row.last_error = error[:2000]
                row.next_attempt_at = _later(retry_in)


def _later(seconds: float):
    from datetime import timedelta

    return utcnow() + timedelta(seconds=seconds)


def _commit_message(payloads: list[dict[str, object]]) -> str:
    summaries = [str(p.get("summary") or "").strip() for p in payloads if p.get("summary")]
    actors = {str(p.get("actor") or "brain") for p in payloads}
    targets = [str(p.get("target") or "") for p in payloads if p.get("target")]
    if len(payloads) == 1:
        subject = f"[{next(iter(actors))}] {summaries[0] if summaries else 'update'}"
    else:
        subject = f"[brain] {len(payloads)} updates: " + "; ".join(summaries[:3])
        if len(summaries) > 3:
            subject += f" (+{len(summaries) - 3} more)"
    body = "\n".join(f"- {t}" for t in targets)
    return subject[:200] + ("\n\n" + body if body else "")


def _author_from(payloads: list[dict[str, object]]) -> str | None:
    actors = {str(p.get("actor")) for p in payloads if p.get("actor")}
    if len(actors) != 1:
        return None
    actor = next(iter(actors))
    agent = actor.split("/", 1)[-1].split("[", 1)[0]
    return f"{actor} <{agent}@brain.local>"
