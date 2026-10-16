from __future__ import annotations

import asyncio
import base64
import fnmatch
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .auth import Principal
from .config import ADMIN_ROLE, SCHEMA_FILE
from .git_sync import mask_remote, normalize_remote
from .llm import LLMClient, LLMConfig, LLMError, extract_json
from .markdown_engine import normalize, parse, slugify, split_lines

if TYPE_CHECKING:
    from .models import Project
    from .service import BrainService

ANALYST = Principal("*", ADMIN_ROLE, "repo-analyst", None, is_master=True)
MAX_FILE_CHARS = 6000
MAX_SQL_CHARS = 3000
MAX_TOTAL_CHARS = 170_000
MAX_TREE_ENTRIES = 400
MAX_SECTION_CHARS = 4000
MAX_FINDINGS = 12
COMMIT_LIMIT = 30
CLONE_DEPTH = 60
SKIP_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "out",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".nuxt",
    ".turbo",
    "coverage",
    "target",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
}
SKIP_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
}
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".mp3",
    ".wav",
    ".mov",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".sqlite",
    ".db",
    ".pyc",
    ".class",
    ".jar",
    ".wasm",
    ".map",
}
PRIORITY_PATTERNS: tuple[tuple[int, str], ...] = (
    (0, "readme*"),
    (0, "docs/readme*"),
    (1, "package.json"),
    (1, "pyproject.toml"),
    (1, "requirements*.txt"),
    (1, "pipfile"),
    (1, "go.mod"),
    (1, "cargo.toml"),
    (1, "pom.xml"),
    (1, "build.gradle*"),
    (1, "composer.json"),
    (1, "gemfile"),
    (1, "mix.exs"),
    (1, "*.csproj"),
    (1, "*/package.json"),
    (1, "*/pyproject.toml"),
    (2, "dockerfile*"),
    (2, "*/dockerfile*"),
    (2, "docker-compose*.y*ml"),
    (2, "compose*.y*ml"),
    (2, "caddyfile"),
    (2, "nginx*.conf"),
    (2, "procfile"),
    (2, "fly.toml"),
    (2, "render.yaml"),
    (2, "railway.*"),
    (2, "vercel.json"),
    (2, "netlify.toml"),
    (2, "serverless.y*ml"),
    (2, ".env.example"),
    (2, ".env.sample"),
    (2, ".github/workflows/*.y*ml"),
    (2, "k8s/*.y*ml"),
    (2, "kubernetes/*.y*ml"),
    (2, "infra/*"),
    (2, "terraform/*.tf"),
    (2, "*.tf"),
    (3, "*schema.prisma"),
    (3, "*/schema.prisma"),
    (3, "*schema.sql"),
    (3, "*/schema.sql"),
    (3, "*/migrations/*"),
    (3, "*/migrate/*"),
    (3, "*/models.py"),
    (3, "*/models/*.py"),
    (3, "*/entities/*"),
    (3, "*/entity/*"),
    (3, "*/schema.ts"),
    (3, "*/schema/*.ts"),
    (3, "*/*.schema.ts"),
    (3, "*/db/*"),
    (3, "*/database/*"),
    (3, "*.sql"),
    (4, "tailwind.config.*"),
    (4, "*/tailwind.config.*"),
    (4, "*theme.*"),
    (4, "*/theme/*"),
    (4, "*tokens.*"),
    (4, "*design-tokens*"),
    (4, "*/colors.*"),
    (4, "*/styles/*.css"),
    (4, "*/styles/*.scss"),
    (4, "*global*.css"),
    (4, "*/app/globals.css"),
    (4, "*/src/index.css"),
    (4, "*/src/app.css"),
    (4, "design.md"),
    (4, "*/design*.md"),
    (4, "brand*.md"),
    (5, "openapi.*"),
    (5, "swagger.*"),
    (5, "*/routes/*"),
    (5, "*/routers/*"),
    (5, "*/api/*"),
    (5, "*/controllers/*"),
    (5, "*/urls.py"),
    (6, "changelog*"),
    (6, "contributing*"),
    (6, "architecture*"),
    (6, "pricing*"),
    (6, "features*"),
    (6, "roadmap*"),
    (6, "product.md"),
    (6, "plan.md"),
    (6, "docs/*.md"),
    (6, "doc/*.md"),
)
PER_BUCKET_LIMIT = {0: 3, 1: 8, 2: 14, 3: 24, 4: 12, 5: 14, 6: 12}
SECTION_HEADINGS: dict[str, tuple[str, ...]] = {
    "DESIGN_SYSTEM.md": (
        "Brand Colors",
        "Typography",
        "Spacing & Layout",
        "Components",
        "Tone of Voice",
        "Do & Don't",
    ),
    "TECH_AND_INFRA.md": (
        "Stack",
        "Services & Endpoints",
        "Deployments",
        "Environments & Secrets Policy",
        "Recent Changes",
    ),
    "COMPANY_PULSE.md": (
        "Mission & Positioning",
        "Pricing Tiers",
        "Shipped Recently",
        "Current Priorities",
        "Open Decisions",
    ),
}
CONTEXT_FILES = ("DESIGN_SYSTEM.md", "TECH_AND_INFRA.md", "COMPANY_PULSE.md", SCHEMA_FILE)
GITHUB_REPO_RE = re.compile(
    r"^(?:https?://[^/]+/|git@[^:]+:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|private[_-]?key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{12,})"
)

SYSTEM_PROMPT = """You are the repository analyst of the AI Computer Brain, the shared memory that keeps a company's AI agents in sync. You receive a digest of one software repository plus the brain's current notes. Produce the Markdown that belongs in the brain so every agent (sales, marketing, engineering, ops, design) works from the same facts.

Return ONE JSON object and nothing else, with this shape:
{
  "summary": "two sentences: what this product is and what the repository contains",
  "sections": [
    {"file": "DESIGN_SYSTEM.md", "heading": "Brand Colors", "content": "markdown body without the heading line", "summary": "one line (max 160 chars) for other agents"}
  ],
  "db_schema": {"engine": {"name": "main", "engine": "postgresql", "version": "", "purpose": ""}, "tables": [{"name": "users", "columns": [{"name": "id", "type": "uuid", "nullable": false, "default": "", "notes": "primary key"}], "indexes": ["users_email_idx"], "notes": ""}]},
  "findings": [{"summary": "max 160 chars, something other departments must know", "affects": ["marketing"], "details": "optional specifics, max 300 chars"}]
}

Allowed files and headings (use these exact strings, nothing else):
- DESIGN_SYSTEM.md: Brand Colors, Typography, Spacing & Layout, Components, Tone of Voice, Do & Don't
- TECH_AND_INFRA.md: Stack, Services & Endpoints, Deployments, Environments & Secrets Policy, Recent Changes
- COMPANY_PULSE.md: Mission & Positioning, Pricing Tiers, Shipped Recently, Current Priorities, Open Decisions

Rules:
- Only write a section when the repository gives real evidence for it. Skip sections you cannot support; never invent colours, prices, services or tables.
- Compare with the current brain content you are given. Return a section only when its content should change; keep what is still true and improve wording, structure and completeness.
- Brand Colors: list tokens with hex values and their use. Typography: families, scale, weights. Components: the reusable UI pieces and their rules.
- Stack: languages, frameworks and versions. Services & Endpoints: the services, their ports/URLs and the main API routes. Deployments: how and where it runs (Docker, platforms, CI). Environments & Secrets Policy: the environment variables by name and how secrets are handled, never their values. Recent Changes: dated bullets from the commit history, newest first, at most 12.
- COMPANY_PULSE.md sections describe the product for non-engineers: positioning, pricing or plans found in the code, features shipped recently, priorities and open decisions visible in TODOs, issues, docs or roadmap files.
- db_schema: only when the repository defines database tables (migrations, ORM models, Prisma, SQL). Otherwise set it to null. Use the real column types.
- findings: 3 to 10 cross-department facts an agent would otherwise miss (an API an integration depends on, a pricing rule in code, a brand rule, a deployment constraint). affects lists departments among engineering, sales, marketing, ops, design, or ["all"].
- Each section body stays under 2500 characters, uses Markdown lists or tables, and contains no secrets: replace any key, token or password with <redacted>.
- Plain ASCII punctuation. No preamble, no code fences around the JSON."""

MERGE_PROMPT = """You maintain one Markdown file of the AI Computer Brain, the shared memory of a company's AI agents. You get the current file and a document the admin uploaded. Rewrite the file so that everything useful from the uploaded document lands under the right existing headings, keeping the title line, the opening paragraph and every existing ## heading in the same order. Update sections whose facts the document changes, extend sections it adds to, leave the others untouched. Keep the Agent Notes section exactly as it is. Replace any secret with <redacted>. Return the complete Markdown file only, with no explanation and no code fence."""


@dataclass(slots=True)
class RepoDigest:
    tree: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    total_files: int = 0
    chars: int = 0


@dataclass(slots=True)
class AnalysisRun:
    project_id: str
    trigger: str
    state: str = "running"
    started_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None
    steps: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] | None = None

    def step(self, text: str) -> None:
        self.steps.append({"at": _now(), "text": text})

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "trigger": self.trigger,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": list(self.steps),
            "error": self.error,
            "result": self.result,
        }


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_repo_url(url: str) -> tuple[str, str]:
    match = GITHUB_REPO_RE.match((url or "").strip())
    if not match:
        raise LLMError("enter a repository URL such as https://github.com/org/repo", 400)
    return match.group(1), match.group(2)


def clone_url_for(url: str) -> str:
    cleaned = (url or "").strip()
    if cleaned.startswith("git@") or cleaned.startswith("ssh://"):
        return cleaned
    if "://" not in cleaned:
        cleaned = "https://" + cleaned
    return cleaned if cleaned.endswith(".git") else cleaned + ".git"


def github_settings(project: Project) -> dict[str, Any]:
    data = project.settings.get("github")
    return dict(data) if isinstance(data, dict) else {}


def describe_github(project: Project, public_url: str) -> dict[str, Any]:
    data = github_settings(project)
    return {
        "connected": bool(data.get("repo_url")),
        "repo_url": data.get("repo_url") or "",
        "branch": data.get("branch") or "",
        "token_set": bool(data.get("token")),
        "analyze_on_push": bool(data.get("analyze_on_push", True)),
        "connected_at": data.get("connected_at"),
        "last_analysis": data.get("last_analysis"),
        "webhook_configured": bool(project.webhook_secret),
        "webhook_url": f"{public_url}/webhooks/{project.project_id}/github",
    }


def _bucket_for(relative: str) -> int | None:
    lowered = relative.casefold()
    name = lowered.rsplit("/", 1)[-1]
    for bucket, pattern in PRIORITY_PATTERNS:
        if fnmatch.fnmatchcase(lowered, pattern) or (
            "/" not in pattern and fnmatch.fnmatchcase(name, pattern)
        ):
            return bucket
    return None


def _is_text(path: Path) -> bool:
    if path.suffix.casefold() in BINARY_SUFFIXES or path.name in SKIP_FILES:
        return False
    try:
        with path.open("rb") as handle:
            sample = handle.read(2048)
    except OSError:
        return False
    return b"\x00" not in sample


def _read_capped(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    if len(text) > limit:
        return text[:limit] + f"\n... ({len(text) - limit} more characters)"
    return text


def collect_digest(root: Path) -> RepoDigest:
    digest = RepoDigest()
    candidates: list[tuple[int, str, Path]] = []
    counts: dict[str, int] = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git"))
        rel_dir = Path(current).relative_to(root).as_posix()
        for name in sorted(files):
            path = Path(current) / name
            relative = f"{rel_dir}/{name}" if rel_dir != "." else name
            digest.total_files += 1
            top = relative.split("/", 1)[0] if "/" in relative else "."
            counts[top] = counts.get(top, 0) + 1
            if len(digest.tree) < MAX_TREE_ENTRIES:
                digest.tree.append(relative)
            bucket = _bucket_for(relative)
            if bucket is not None and _is_text(path):
                candidates.append((bucket, relative, path))
    if digest.total_files > MAX_TREE_ENTRIES:
        digest.tree.append(
            f"... {digest.total_files - MAX_TREE_ENTRIES} more files; per top-level dir: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
    candidates.sort(key=lambda item: (item[0], item[1].count("/"), item[1]))
    used: dict[int, int] = {}
    for bucket, relative, path in candidates:
        if used.get(bucket, 0) >= PER_BUCKET_LIMIT.get(bucket, 8):
            continue
        limit = MAX_SQL_CHARS if path.suffix.casefold() == ".sql" else MAX_FILE_CHARS
        text = _read_capped(path, limit)
        if not text.strip():
            continue
        if digest.chars + len(text) > MAX_TOTAL_CHARS:
            break
        digest.files.append((relative, text))
        digest.chars += len(text)
        used[bucket] = used.get(bucket, 0) + 1
    return digest


def build_user_prompt(
    project_name: str, repo_url: str, digest: RepoDigest, current: dict[str, str]
) -> str:
    parts = [
        f"Project: {project_name}",
        f"Repository: {mask_remote(repo_url)}",
        "",
        f"## File tree ({digest.total_files} files)",
        "\n".join(digest.tree),
        "",
        "## Recent commits (newest first)",
        "\n".join(digest.commits) or "(no history available)",
        "",
        "## Current brain content",
    ]
    for name, text in current.items():
        parts.extend([f"### {name}", text.strip() or "(empty)", ""])
    parts.append("## Repository files")
    for relative, text in digest.files:
        parts.extend([f"### {relative}", text, ""])
    return "\n".join(parts)


def _section_target(file_name: str, heading: str) -> tuple[str, str] | None:
    headings = SECTION_HEADINGS.get((file_name or "").strip())
    if not headings:
        return None
    wanted = slugify((heading or "").strip())
    for candidate in headings:
        if slugify(candidate) == wanted:
            return file_name.strip(), candidate
    return None


def _clean_summary(text: Any, fallback: str) -> str:
    cleaned = " ".join(str(text or "").split())
    return (cleaned or fallback)[:160]


def _departments(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        token = str(value).strip().casefold()
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned[:6]


class RepoAnalyzer:
    def __init__(self, service: BrainService) -> None:
        self.service = service
        self.runs: dict[str, AnalysisRun] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._rerun: set[str] = set()

    @property
    def settings(self) -> Any:
        return self.service.settings

    def status(self, project_id: str) -> dict[str, Any]:
        run = self.runs.get(project_id)
        if run is None:
            return {
                "project_id": project_id,
                "state": "idle",
                "steps": [],
                "result": None,
                "error": None,
            }
        return run.to_dict()

    def is_running(self, project_id: str) -> bool:
        run = self.runs.get(project_id)
        return run is not None and run.state == "running"

    def auto_trigger(self, project: Project, clone_url: str) -> bool:
        data = github_settings(project)
        if not data.get("repo_url") or not data.get("analyze_on_push", True):
            return False
        if not LLMConfig.from_settings(self.settings).enabled:
            return False
        return normalize_remote(data["repo_url"]) == normalize_remote(clone_url)

    def start(self, project_id: str, trigger: str = "manual") -> bool:
        if self.is_running(project_id):
            self._rerun.add(project_id)
            return False
        self.service.spawn(self.run(project_id, trigger))
        return True

    async def run(self, project_id: str, trigger: str = "manual") -> dict[str, Any]:
        lock = self._locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            run = AnalysisRun(project_id, trigger)
            self.runs[project_id] = run
            try:
                result = await self._execute(project_id, run)
                run.result = result
                run.state = "done"
            except LLMError as error:
                run.error = error.detail
                run.state = "failed"
            except Exception as error:
                run.error = f"{error.__class__.__name__}: {error}"
                run.state = "failed"
            run.finished_at = _now()
            await self._remember(project_id, run)
        if project_id in self._rerun:
            self._rerun.discard(project_id)
            self.service.spawn(self.run(project_id, "push"))
        return run.to_dict()

    async def _execute(self, project_id: str, run: AnalysisRun) -> dict[str, Any]:
        project = await self.service.get_project(project_id)
        data = github_settings(project)
        repo_url = data.get("repo_url")
        if not repo_url:
            raise LLMError("connect a GitHub repository first", 409)
        config = LLMConfig.from_settings(self.settings)
        client = LLMClient(config)
        await self._note(run, f"Fetching {mask_remote(repo_url)}")
        sha, branch = await self._sync_clone(
            project_id, repo_url, data.get("token") or "", data.get("branch") or ""
        )
        root = self.settings.repos_dir / project_id
        await self._note(run, f"Reading the repository at {sha[:7]} on {branch}")
        digest = await asyncio.to_thread(collect_digest, root)
        digest.commits = await self._commits(root)
        await self._note(
            run,
            f"Selected {len(digest.files)} of {digest.total_files} files ({digest.chars // 1000}k characters)",
        )
        current: dict[str, str] = {}
        vault = self.service.vault(project)
        for name in CONTEXT_FILES:
            try:
                current[name] = (await vault.read(name))[:8000]
            except OSError:
                current[name] = ""
        await self._note(run, f"Asking {config.resolved_model} to analyse the repository")
        started = time.monotonic()
        raw = await client.complete(
            SYSTEM_PROMPT, build_user_prompt(project.project_name, repo_url, digest, current), 24000
        )
        plan = extract_json(raw)
        await self._note(
            run, f"Model answered in {round(time.monotonic() - started)}s; applying to the brain"
        )
        applied = await self._apply(project, plan, run)
        applied.update(
            {
                "sha": sha,
                "branch": branch,
                "model": config.resolved_model,
                "files_read": len(digest.files),
                "summary": " ".join(str(plan.get("summary") or "").split())[:600],
            }
        )
        await self.service.system_event(
            project_id,
            "repo_analysis",
            f"Analysed {mask_remote(repo_url)} @{sha[:7]}: {applied['sections_updated']} sections, "
            f"{applied['findings']} findings, {applied['tables']} tables",
        )
        await self._note(
            run,
            f"Done: {applied['sections_updated']} sections updated, {applied['findings']} findings, "
            f"{applied['tables']} tables synced",
        )
        return applied

    async def _apply(
        self, project: Project, plan: dict[str, Any], run: AnalysisRun
    ) -> dict[str, Any]:
        updated: list[str] = []
        skipped = 0
        errors: list[str] = []
        sections = plan.get("sections") if isinstance(plan.get("sections"), list) else []
        for item in sections:
            if not isinstance(item, dict):
                continue
            target = _section_target(str(item.get("file") or ""), str(item.get("heading") or ""))
            content = str(item.get("content") or "").strip()
            if target is None or not content:
                skipped += 1
                continue
            file_name, heading = target
            content = content[:MAX_SECTION_CHARS]
            summary = _clean_summary(item.get("summary"), f"{heading} updated from the repository")
            if await self._unchanged(project, file_name, heading, content):
                skipped += 1
                continue
            try:
                await self._write(project, file_name, heading, content, summary)
                updated.append(f"{file_name}#{slugify(heading)}")
            except Exception as error:
                errors.append(f"{file_name}#{heading}: {getattr(error, 'detail', error)}")
        tables = 0
        schema = plan.get("db_schema")
        if isinstance(schema, dict) and isinstance(schema.get("tables"), list) and schema["tables"]:
            try:
                result = await self.service.sync_db_schema(
                    ANALYST, project, schema, "Database schema synced from the repository"
                )
                tables = len(result.get("changed_tables") or []) if isinstance(result, dict) else 0
            except Exception as error:
                errors.append(f"db_schema: {getattr(error, 'detail', error)}")
        findings = 0
        for item in (plan.get("findings") or [])[:MAX_FINDINGS]:
            if not isinstance(item, dict):
                continue
            summary = _clean_summary(item.get("summary"), "")
            if not summary:
                continue
            try:
                outcome = await self.service.log_activity(
                    ANALYST,
                    project,
                    summary,
                    "finding",
                    _departments(item.get("affects")) or None,
                    str(item.get("details") or "")[:300] or None,
                    "normal",
                )
                if outcome.get("status") == "ok":
                    findings += 1
            except Exception as error:
                errors.append(f"finding: {getattr(error, 'detail', error)}")
        if errors:
            await self._note(
                run, f"{len(errors)} items could not be applied: " + "; ".join(errors)[:300]
            )
        return {
            "sections_updated": len(updated),
            "sections": updated,
            "sections_skipped": skipped,
            "findings": findings,
            "tables": tables,
            "errors": errors,
        }

    async def _unchanged(
        self, project: Project, file_name: str, heading: str, content: str
    ) -> bool:
        try:
            doc = await self.service.vault(project).document(file_name)
        except OSError:
            return False
        section = doc.find(heading)
        if section is None:
            return False
        return normalize(split_lines(doc.body(section))) == normalize(split_lines(content))

    async def _write(
        self, project: Project, file_name: str, heading: str, content: str, summary: str
    ) -> None:
        from .service import BrainError

        try:
            await self.service.update_section(
                ANALYST, project, file_name, heading, content, summary, "replace"
            )
        except BrainError as error:
            if error.code != "NOT_FOUND":
                raise
            await self.service.update_section(
                ANALYST, project, file_name, heading, content, summary, "create"
            )

    async def _note(self, run: AnalysisRun, text: str) -> None:
        run.step(text)
        from .hub import Event

        await self.service.hub.publish(
            Event(
                "repo_analysis",
                run.project_id,
                agent_id="repo-analyst",
                department="system",
                summary=text[:160],
                importance="low",
            )
        )

    async def _remember(self, project_id: str, run: AnalysisRun) -> None:
        try:
            project = await self.service.get_project(project_id)
        except Exception:
            return
        data = github_settings(project)
        data["last_analysis"] = {
            "finished_at": run.finished_at,
            "ok": run.state == "done",
            "trigger": run.trigger,
            "error": run.error,
            "summary": (run.result or {}).get("summary"),
            "sections_updated": (run.result or {}).get("sections_updated", 0),
            "findings": (run.result or {}).get("findings", 0),
            "tables": (run.result or {}).get("tables", 0),
            "sha": (run.result or {}).get("sha"),
            "model": (run.result or {}).get("model"),
        }
        settings = dict(project.settings)
        settings["github"] = data
        await self.service.update_project(project_id, settings=settings)

    async def _sync_clone(
        self, project_id: str, repo_url: str, token: str, branch: str
    ) -> tuple[str, str]:
        dest = self.settings.repos_dir / project_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = clone_url_for(repo_url)
        auth = _auth_args(url, token)
        if (dest / ".git").is_dir():
            try:
                await _git(dest, *auth, "remote", "set-url", "origin", url)
                fetch = ["fetch", "-q", f"--depth={CLONE_DEPTH}", "origin"]
                if branch:
                    fetch.append(branch)
                await _git(dest, *auth, *fetch)
                target = f"origin/{branch}" if branch else "FETCH_HEAD"
                await _git(dest, "reset", "-q", "--hard", target)
            except LLMError:
                await asyncio.to_thread(_remove_tree, dest)
        if not (dest / ".git").is_dir():
            args = ["clone", "-q", f"--depth={CLONE_DEPTH}"]
            if branch:
                args.extend(["--branch", branch])
            await _git(dest.parent, *auth, *args, url, str(dest))
        sha = (await _git(dest, "rev-parse", "HEAD")).strip()
        current = (await _git(dest, "rev-parse", "--abbrev-ref", "HEAD")).strip()
        return sha, branch or (current if current != "HEAD" else "detached")

    async def _commits(self, root: Path) -> list[str]:
        try:
            output = await _git(
                root, "log", f"-{COMMIT_LIMIT}", "--date=short", "--pretty=format:%ad %an: %s"
            )
        except LLMError:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]


def _auth_args(url: str, token: str) -> list[str]:
    args = ["-c", "credential.helper=", "-c", "core.askPass="]
    if not token or not url.startswith("http"):
        return args
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return [*args, "-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]


async def _git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise LLMError(f"git: {message[:300]}", 502)
    return stdout.decode("utf-8", "replace")


def _remove_tree(path: Path) -> None:
    def _onerror(func: Any, target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


async def smart_merge(config: LLMConfig, file_name: str, current: str, uploaded: str) -> str:
    client = LLMClient(config)
    prompt = (
        f"File name: {file_name}\n\n## Current file\n{current.strip()}\n\n"
        f"## Uploaded document\n{uploaded.strip()[:60000]}"
    )
    merged = await client.complete(MERGE_PROMPT, prompt, 24000).strip()
    merged = re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", "", merged)
    doc = parse(merged)
    if len(doc.sections) < 2 or not merged.lstrip().startswith("#"):
        raise LLMError(
            "the model did not return a valid brain file; upload with mode replace instead", 502
        )
    return merged if merged.endswith("\n") else merged + "\n"


def summarize_upload(file_name: str, original_name: str, mode: str) -> str:
    return (
        f"{file_name} {'merged from' if mode == 'smart' else 'replaced by upload'} {original_name}"[
            :160
        ]
    )


__all__ = [
    "ANALYST",
    "AnalysisRun",
    "RepoAnalyzer",
    "RepoDigest",
    "collect_digest",
    "build_user_prompt",
    "describe_github",
    "github_settings",
    "parse_repo_url",
    "clone_url_for",
    "smart_merge",
    "summarize_upload",
]
