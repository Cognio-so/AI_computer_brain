from __future__ import annotations

import os
import re
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
HOME_ENV = "BRAIN_HOME"


def default_home() -> Path:
    return Path.home() / ".brain"


def brain_home() -> Path:
    explicit = os.environ.get(HOME_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    cwd = Path.cwd()
    if (cwd / ".env").is_file():
        return cwd
    return default_home()


def enter_home() -> Path:
    home = brain_home()
    home.mkdir(parents=True, exist_ok=True)
    if Path.cwd().resolve() != home.resolve():
        os.chdir(home)
    return home


CANONICAL_FILES: tuple[str, ...] = (
    "DESIGN_SYSTEM.md",
    "TECH_AND_INFRA.md",
    "DB_SCHEMA_STATE.md",
    "COMPANY_LOG.md",
    "ACCESS_PERMISSIONS.md",
    "COMPANY_PULSE.md",
    "AGENT_CHANGELOG.md",
)
LOG_FILE = "COMPANY_LOG.md"
PERMISSIONS_FILE = "ACCESS_PERMISSIONS.md"
SCHEMA_FILE = "DB_SCHEMA_STATE.md"
CHANGELOG_FILE = "AGENT_CHANGELOG.md"
DIGEST_HEADING = "Daily Digest"
DEFAULT_ROLES: tuple[str, ...] = ("admin", "engineering", "sales", "marketing", "ops", "design")
ADMIN_ROLE = "admin"
IMPORTANCE_LEVELS: tuple[str, ...] = ("low", "normal", "high")
ACTIVITY_KINDS: tuple[str, ...] = ("task", "finding", "decision", "blocker")
KEY_PREFIX = "sb"
CHANGELOG_ENTRIES_PER_DEPARTMENT = 100
DIGEST_DAYS = 7

IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38})[a-z0-9]$")
LLM_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "bedrock",
    "custom",
)


def is_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value))


def generate_master_key() -> str:
    return "bm_" + secrets.token_urlsafe(36)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def update_env(path: Path, **values: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    rewritten: list[str] = []
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else None
        )
        if key in pending:
            rewritten.append(f"{key}={pending.pop(key)}")
        else:
            rewritten.append(line)
    rewritten.extend(f"{key}={value}" for key, value in pending.items())
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def _first_existing(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    brain_host: str = "0.0.0.0"
    brain_port: int = 8000
    public_url: str = "http://localhost:8000"
    brain_domain: str = ""
    brain_theme: str = ""
    admin_master_key: str = ""
    admin_path: str = "/admin"
    admin_allowed_ips: str = ""
    admin_login_failures_per_minute: int = 10
    database_url: str = "sqlite+aiosqlite:///./data/brain_metadata.db"
    redis_url: str = ""
    brain_files_dir: Path = Path("./brain_files")
    git_author_name: str = "AI Computer Brain"
    git_author_email: str = "brain@localhost"
    git_push_enabled: bool = True
    git_commit_debounce_seconds: float = 2.0
    hot_log_size: int = 50
    inbox_inline_limit: int = 3
    stale_after_minutes: int = 30
    max_patch_bytes: int = 65536
    max_summary_chars: int = 160
    rate_limit_writes_per_minute: int = 120
    rate_limit_reads_per_minute: int = 600
    heartbeat_seconds: int = 20
    changelog_entries_per_department: int = CHANGELOG_ENTRIES_PER_DEPARTMENT
    findings_in_company_log: int = 30
    changelog_dedupe_hours: int = 24
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    llm_auth: str = "api_key"
    llm_effort: str = ""
    cloudflare_tunnel: str = ""
    github_token: str = ""
    github_owner: str = ""
    disk_watch_seconds: int = 60
    cors_origins: str = ""
    log_level: str = "info"

    @field_validator("brain_files_dir", mode="after")
    @classmethod
    def _absolute_files_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("public_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("llm_provider", mode="after")
    @classmethod
    def _normalise_provider(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        return cleaned if cleaned in LLM_PROVIDERS else ""

    @field_validator("llm_auth", mode="after")
    @classmethod
    def _normalise_auth(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        return cleaned if cleaned in ("login", "oauth") else "api_key"

    @field_validator("admin_path", mode="after")
    @classmethod
    def _normalise_admin_path(cls, value: str) -> str:
        cleaned = "/" + value.strip().strip("/")
        return cleaned if cleaned != "/" else "/admin"

    @model_validator(mode="after")
    def _align_local_public_url(self) -> Settings:
        match = re.match(r"^(http://(?:localhost|127\.0\.0\.1))(?::\d+)?$", self.public_url)
        if match:
            self.public_url = f"{match.group(1)}:{self.brain_port}"
        return self

    @property
    def templates_dir(self) -> Path:
        return _first_existing(PACKAGE_DIR / "templates", REPO_ROOT / "templates")

    @property
    def vault_template_dir(self) -> Path:
        return self.templates_dir / "default_vault"

    @property
    def admin_dashboard_path(self) -> Path:
        return self.templates_dir / "admin_dashboard.html"

    @property
    def sql_dir(self) -> Path:
        return _first_existing(PACKAGE_DIR / "sql", REPO_ROOT / "sql")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def admin_allowed_networks(self) -> list[str]:
        return [item.strip() for item in self.admin_allowed_ips.split(",") if item.strip()]

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_master_key)

    @property
    def llm_enabled(self) -> bool:
        if not self.llm_provider:
            return False
        if self.llm_provider == "anthropic" and self.llm_auth == "login":
            return True
        if self.llm_auth == "oauth" and self.llm_provider in ("anthropic", "openai", "gemini"):
            return True
        return bool(self.llm_api_key)

    @property
    def oauth_store_path(self) -> Path:
        sqlite_path = self.sqlite_path
        if sqlite_path is not None and sqlite_path.name != ":memory:":
            return sqlite_path.parent / "oauth.json"
        return self.brain_files_dir / ".oauth.json"

    @property
    def repos_dir(self) -> Path:
        return self.brain_files_dir / ".repos"

    def env_path(self) -> Path | None:
        candidate = Path.cwd() / ".env"
        return candidate if candidate.is_file() else None

    @property
    def sqlite_path(self) -> Path | None:
        marker = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(marker):
            return None
        raw = self.database_url[len(marker) :]
        return Path(raw).expanduser()

    def ensure_directories(self) -> None:
        self.brain_files_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = self.sqlite_path
        if sqlite_path is not None and sqlite_path.name != ":memory:":
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
