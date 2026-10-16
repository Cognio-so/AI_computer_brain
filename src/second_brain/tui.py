from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__, background, cloudflare
from .banner import print_logo
from .config import (
    CANONICAL_FILES,
    Settings,
    generate_master_key,
    get_settings,
    read_env,
    reset_settings_cache,
    update_env,
)
from .picker import Choice, choose

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
EDITABLE_KEYS = (
    "PUBLIC_URL",
    "BRAIN_DOMAIN",
    "CLOUDFLARE_TUNNEL",
    "BRAIN_HOST",
    "BRAIN_PORT",
    "BRAIN_THEME",
    "ADMIN_PATH",
    "ADMIN_ALLOWED_IPS",
    "ADMIN_LOGIN_FAILURES_PER_MINUTE",
    "DATABASE_URL",
    "REDIS_URL",
    "BRAIN_FILES_DIR",
    "GIT_PUSH_ENABLED",
    "STALE_AFTER_MINUTES",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LOG_LEVEL",
)
SECRET_KEYS = ("ADMIN_MASTER_KEY", "LLM_API_KEY")
FILE_HINTS: tuple[tuple[str, str], ...] = (
    ("design", "DESIGN_SYSTEM.md"),
    ("brand", "DESIGN_SYSTEM.md"),
    ("style", "DESIGN_SYSTEM.md"),
    ("tech", "TECH_AND_INFRA.md"),
    ("infra", "TECH_AND_INFRA.md"),
    ("stack", "TECH_AND_INFRA.md"),
    ("schema", "DB_SCHEMA_STATE.md"),
    ("database", "DB_SCHEMA_STATE.md"),
    ("db", "DB_SCHEMA_STATE.md"),
    ("pulse", "COMPANY_PULSE.md"),
    ("company", "COMPANY_PULSE.md"),
    ("product", "COMPANY_PULSE.md"),
    ("permission", "ACCESS_PERMISSIONS.md"),
    ("access", "ACCESS_PERMISSIONS.md"),
    ("rules", "ACCESS_PERMISSIONS.md"),
    ("changelog", "AGENT_CHANGELOG.md"),
)
SKIP_WALK = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
BARE_WORDS = {"help": "help", "?": "help", "quit": "quit", "exit": "quit", "q": "quit"}
BOOLEAN_FLAGS = {"smart", "all"}


@dataclass(frozen=True, slots=True)
class Palette:
    key: str
    name: str
    tagline: str
    primary: str
    accent: str
    success: str
    warning: str
    danger: str
    muted: str


PALETTES: tuple[Palette, ...] = (
    Palette(
        "ocean",
        "Ocean",
        "cool blues and violet",
        "#38bdf8",
        "#a78bfa",
        "#34d399",
        "#fbbf24",
        "#f87171",
        "#94a3b8",
    ),
    Palette(
        "ember",
        "Ember",
        "warm orange and pink",
        "#fb923c",
        "#f472b6",
        "#4ade80",
        "#facc15",
        "#ef4444",
        "#a8a29e",
    ),
    Palette(
        "forest",
        "Forest",
        "greens and teal",
        "#4ade80",
        "#2dd4bf",
        "#86efac",
        "#fde047",
        "#f87171",
        "#a3a3a3",
    ),
    Palette(
        "violet",
        "Violet",
        "purple and sky",
        "#c084fc",
        "#60a5fa",
        "#34d399",
        "#fbbf24",
        "#fb7185",
        "#a1a1aa",
    ),
    Palette(
        "sunset",
        "Sunset",
        "pink, orange, gold",
        "#f472b6",
        "#fb923c",
        "#4ade80",
        "#fde047",
        "#ef4444",
        "#d4d4d8",
    ),
    Palette(
        "mint",
        "Mint",
        "aqua and lime",
        "#2dd4bf",
        "#a3e635",
        "#4ade80",
        "#fbbf24",
        "#fb7185",
        "#94a3b8",
    ),
    Palette(
        "gold",
        "Gold",
        "amber and bronze",
        "#fbbf24",
        "#f97316",
        "#4ade80",
        "#fde68a",
        "#ef4444",
        "#a8a29e",
    ),
    Palette(
        "mono",
        "Mono",
        "plain white, no colour",
        "white",
        "bright_white",
        "green",
        "yellow",
        "red",
        "grey66",
    ),
)
DEFAULT_THEME = PALETTES[0].key

current: Palette = PALETTES[0]
console: Console = Console(highlight=False)


def palette_for(key: str | None) -> Palette:
    wanted = (key or "").strip().casefold()
    for palette in PALETTES:
        if palette.key == wanted:
            return palette
    return PALETTES[0]


def rich_theme(palette: Palette) -> Theme:
    return Theme(
        {
            "primary": f"bold {palette.primary}",
            "accent": f"bold {palette.accent}",
            "success": f"bold {palette.success}",
            "warning": f"bold {palette.warning}",
            "danger": f"bold {palette.danger}",
            "muted": palette.muted,
            "key": f"bold {palette.accent}",
            "label": f"bold {palette.primary}",
            "border": palette.primary,
            "title": f"bold {palette.primary}",
            "value": "default",
        }
    )


def apply_theme(key: str | None) -> Palette:
    global console, current
    current = palette_for(key)
    console = Console(highlight=False, theme=rich_theme(current))
    return current


def safe_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def swatch(palette: Palette) -> Text:
    text = Text()
    for color in (
        palette.primary,
        palette.accent,
        palette.success,
        palette.warning,
        palette.danger,
    ):
        text.append("  ", style=f"on {color}")
    text.append("  ")
    text.append("Admin panel ", style=f"bold {palette.primary}")
    text.append("API key ", style=f"bold {palette.accent}")
    text.append("ok ", style=palette.success)
    text.append("stale ", style=palette.warning)
    text.append("error", style=palette.danger)
    return text


def choose_theme(current_key: str | None = None) -> str:
    default_index = 0
    choices = []
    for index, palette in enumerate(PALETTES):
        if palette.key == (current_key or "").casefold():
            default_index = index
        choices.append(Choice(palette.name, palette.key, palette.tagline, swatch(palette)))
    console.print()
    console.print(
        Text("The logo, the console and the admin panel use the theme colours.", style="dim")
    )
    picked = choose(console, "Pick a colour theme", choices, default_index, ask=prompt)
    return picked.value if picked else palette_for(current_key).key


def mask(value: str, key: str = "") -> str:
    if key in SECRET_KEYS and value:
        return f"{value[:3]}...{value[-4:]}" if len(value) > 8 else "set"
    if key in ("DATABASE_URL", "REDIS_URL"):
        return re.sub(r"://([^/@:]+)(:[^/@]*)?@", r"://\1:***@", value)
    return value


def prompt(text: str, default: str = "", hide: bool = False) -> str:
    answer = typer.prompt(text, default=default, show_default=bool(default), hide_input=hide)
    return str(answer).strip()


def confirm(text: str, default: bool = False) -> bool:
    return bool(typer.confirm(text, default=default))


def read_input(prompt_markup: str) -> str:
    return console.input(prompt_markup)


def panel(body: Any, title: str, subtitle: str | None = None, style: str = "border") -> Panel:
    return Panel(
        body,
        title=f"[{style}]{title}[/{style}]",
        subtitle=f"[muted]{subtitle}[/muted]" if subtitle else None,
        border_style=style,
        expand=False,
        padding=(0, 1),
    )


def grid() -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="label")
    table.add_column()
    return table


def listing(title: str, *columns: str) -> Table:
    table = Table(
        title=f"[title]{title}[/title]",
        box=None,
        header_style="label",
        pad_edge=False,
        title_justify="left",
    )
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def tokenize(line: str) -> list[str]:
    tokens: list[str] = []
    current_token = ""
    quote: str | None = None
    for char in line:
        if quote:
            if char == quote:
                quote = None
            else:
                current_token += char
        elif char in ('"', "'"):
            quote = char
        elif char.isspace():
            if current_token:
                tokens.append(current_token)
                current_token = ""
        else:
            current_token += char
    if current_token:
        tokens.append(current_token)
    return tokens


def split_options(args: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-p", "--project") and index + 1 < len(args):
            options["project"] = args[index + 1]
            index += 2
            continue
        if token.startswith("--"):
            name, _, value = token[2:].partition("=")
            if not value and name not in BOOLEAN_FLAGS and index + 1 < len(args):
                following = args[index + 1]
                if not following.startswith("-"):
                    value = following
                    index += 1
            options[name] = value or "true"
        else:
            positional.append(token)
        index += 1
    return positional, options


def guess_file(name: str) -> str | None:
    lowered = Path(name).name.casefold()
    for hint, target in FILE_HINTS:
        if hint in lowered:
            return target
    upper = Path(name).name.upper()
    return upper if upper in CANONICAL_FILES else None


def markdown_lines(text: str) -> Text:
    out = Text()
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(line + "\n", style="muted")
        elif in_code:
            out.append(line + "\n", style="muted")
        elif stripped.startswith("#"):
            out.append(line + "\n", style="primary")
        elif stripped.startswith(("- ", "* ")):
            out.append(line[: len(line) - len(line.lstrip())], style="")
            out.append("- ", style="accent")
            out.append(stripped[2:] + "\n")
        elif stripped.startswith("|"):
            out.append(line + "\n", style="muted" if set(stripped) <= set("|-: ") else "")
        else:
            out.append(line + "\n")
    return out


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    usage: str
    help: str
    group: str
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[Command, ...] = (
    Command("help", "/help [command]", "Show this list, or details for one command", "Console"),
    Command(
        "status", "/status", "URLs, master key, projects, LLM and GitHub at a glance", "Console"
    ),
    Command("menu", "/menu", "Guided numbered menu for the common tasks", "Console"),
    Command("open", "/open", "Open the admin panel in your browser, signed in", "Console"),
    Command("theme", "/theme [name]", "Change the colour theme", "Console"),
    Command("clear", "/clear", "Clear the screen", "Console", ("cls",)),
    Command("quit", "/quit", "Stop the server and leave", "Console", ("exit", "q")),
    Command(
        "projects", "/projects", "List projects with their keys and live agents", "Brain", ("p",)
    ),
    Command(
        "new", "/new <company name>", "Create a project and its first API key", "Brain", ("create",)
    ),
    Command("use", "/use <project>", "Set the default project for the other commands", "Brain"),
    Command("delete", "/delete [project]", "Delete a project in two steps", "Brain"),
    Command(
        "backup",
        "/backup [project]",
        "Create the <project>_brain backup repository on your GitHub and push every change there",
        "Sync",
    ),
    Command(
        "key",
        "/key <what it is for> [-p project]",
        "Create an API key for one agent",
        "Brain",
        ("add",),
    ),
    Command("keys", "/keys [project]", "List the API keys of a project", "Brain"),
    Command(
        "agent",
        "/agent [agent id] [-p project]",
        "Connection commands for one agent again, and a replacement key when the old one is lost",
        "Brain",
    ),
    Command("revoke", "/revoke [project]", "Revoke an API key", "Brain"),
    Command("files", "/files [project]", "The brain files with their sections and etags", "Brain"),
    Command("show", "/show <FILE.md> [project]", "Print a brain file", "Brain", ("view",)),
    Command(
        "upload",
        "/upload <path> [FILE.md] [-p project] [--smart]",
        "Upload a Markdown file into the brain (--smart merges it with the LLM)",
        "Brain",
    ),
    Command(
        "activity",
        "/activity [project]",
        "Latest changes made by agents and humans",
        "Brain",
        ("log",),
    ),
    Command(
        "follow",
        "/follow [project]",
        "Stream changes live until you press Enter",
        "Brain",
        ("tail",),
    ),
    Command("agents", "/agents [project]", "Agents connected right now and sync health", "Brain"),
    Command(
        "docs",
        "/docs [add <path> [--title t] [--agents a,b|--roles r] | rm | share | read] [-p project]",
        "Knowledge base: upload documents and choose which agents may read them",
        "Brain",
        ("doc", "documents"),
    ),
    Command("github", "/github [project] [off]", "Connect the company GitHub repository", "Sync"),
    Command(
        "analyze",
        "/analyze [project]",
        "Analyse the repository with the LLM and fill the brain",
        "Sync",
        ("analyse",),
    ),
    Command(
        "llm",
        "/llm [test|off]",
        "Connect OpenAI, Claude (sign-in or key), Gemini, OpenRouter, Bedrock or another provider",
        "Sync",
    ),
    Command(
        "chat",
        "/chat [question]",
        "Ask the assistant about the brain, or tell it what to change in a brain file",
        "Sync",
        ("ask",),
    ),
    Command("connect", "/connect", "Domain, Cloudflare tunnel or hosting options", "Server"),
    Command(
        "tunnel", "/tunnel", "Expose this computer through a Cloudflare quick tunnel", "Server"
    ),
    Command(
        "domain",
        "/domain [name] | vps | hosting | forget",
        "One-command Cloudflare deploy of your domain or subdomain from this computer; vps or hosting prints those steps; forget removes a saved domain",
        "Server",
    ),
    Command(
        "service",
        "/service [install|status|logs|stop|start|uninstall]",
        "Run the brain in the background: starts at login or boot, stays up when this console closes",
        "Server",
    ),
    Command("security", "/security", "Master key, admin path, allowed IPs, lockout", "Server"),
    Command("protect", "/protect", "Lock the admin panel down (path, IPs, lockout)", "Server"),
    Command("master", "/master", "Show the master key", "Server"),
    Command("rotate", "/rotate", "Generate a new master key", "Server"),
    Command("env", "/env", "Show the settings in .env", "Server", ("settings",)),
    Command("set", "/set KEY value", "Change one setting and restart", "Server"),
    Command("edit", "/edit", "Open .env in your editor", "Server"),
    Command("restart", "/restart", "Restart the server", "Server"),
)
COMMAND_INDEX: dict[str, Command] = {}
for _command in COMMANDS:
    COMMAND_INDEX[_command.name] = _command
    for _alias in _command.aliases:
        COMMAND_INDEX[_alias] = _command


class ServerHandle:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.settings.brain_port

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        from .server import create_app

        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        for name in ("httpx", "httpcore", "mcp", "uvicorn", "brain"):
            logging.getLogger(name).setLevel(logging.WARNING)
        app = create_app(self.settings)
        config = uvicorn.Config(
            app,
            host=self.settings.brain_host,
            port=self.settings.brain_port,
            log_level="warning",
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="*",
            timeout_keep_alive=75,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="brain-server", daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.thread is not None and not self.thread.is_alive():
                raise RuntimeError(f"the server stopped; is port {self.port} already in use?")
            try:
                if httpx.get(f"{self.local_url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise RuntimeError("the server did not become ready in time")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)
        self.server = None
        self.thread = None

    def restart(self, settings: Settings) -> None:
        self.stop()
        self.settings = settings
        self.start()


class AdminClient:
    def __init__(self, base_url: str, master_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self.http = httpx.Client(
            base_url=self.base_url, headers={"Authorization": f"Bearer {master_key}"}, timeout=30
        )

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = {"error": response.text}
            raise RuntimeError(detail.get("error") or detail.get("detail") or response.text)
        return response.json() if response.content else None

    def status(self) -> dict[str, Any]:
        return self._json(self.http.get("/api/admin/status"))

    def projects(self) -> list[dict[str, Any]]:
        return self._json(self.http.get("/api/admin/projects"))

    def quick_project(
        self,
        name: str,
        first_agent: str | None = None,
        repo_url: str | None = None,
        backup: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "backup": backup}
        if first_agent:
            body["first_agent"] = first_agent
        if repo_url:
            body["repo_url"] = repo_url
        return self._json(self.http.post("/api/admin/projects/quick", json=body, timeout=120))

    def github_account(self) -> dict[str, Any]:
        return self._json(self.http.get("/api/admin/github-account"))

    def connect_github_account(self, token: str) -> dict[str, Any]:
        return self._json(self.http.put("/api/admin/github-account", json={"token": token}))

    def create_backup(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.post(f"/api/admin/projects/{project_id}/backup", timeout=120))

    def documents(self, project_id: str) -> list[dict[str, Any]]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/documents"))

    def document(self, project_id: str, doc_id: str) -> dict[str, Any]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/documents/{doc_id}"))

    def add_document(
        self, project_id: str, path: Path, title: str = "", access: str = "*"
    ) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self._json(
                self.http.post(
                    f"/api/admin/projects/{project_id}/documents",
                    files={"upload": (path.name, handle, "application/octet-stream")},
                    data={"title": title, "access": access},
                    timeout=300,
                )
            )

    def update_document(self, project_id: str, doc_id: str, **fields: Any) -> dict[str, Any]:
        return self._json(
            self.http.put(f"/api/admin/projects/{project_id}/documents/{doc_id}", json=fields)
        )

    def delete_document(self, project_id: str, doc_id: str) -> dict[str, Any]:
        return self._json(self.http.delete(f"/api/admin/projects/{project_id}/documents/{doc_id}"))

    def quick_key(
        self, name: str, project_id: str | None = None, project_name: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if project_id:
            body["project_id"] = project_id
        if project_name:
            body["project_name"] = project_name
        return self._json(self.http.post("/api/admin/keys/quick", json=body))

    def keys(self, project_id: str) -> list[dict[str, Any]]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/keys"))

    def revoke(self, project_id: str, key_id: str) -> dict[str, Any]:
        return self._json(self.http.delete(f"/api/admin/projects/{project_id}/keys/{key_id}"))

    def connect_info(self, project_id: str, key_id: str) -> dict[str, Any]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/keys/{key_id}/connect"))

    def reissue(self, project_id: str, key_id: str) -> dict[str, Any]:
        return self._json(self.http.post(f"/api/admin/projects/{project_id}/keys/{key_id}/reissue"))

    def reseal_keys(self, master_key: str) -> dict[str, Any]:
        return self._json(self.http.post("/api/admin/keys/reseal", json={"master_key": master_key}))

    def recent(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._json(
            self.http.get(f"/api/admin/projects/{project_id}/log/recent?limit={limit}")
        )

    def sessions(self, project_id: str) -> list[dict[str, Any]]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/sessions"))

    def webhook_secret(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.post(f"/api/admin/projects/{project_id}/webhook-secret"))

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        return self._json(self.http.put(f"/api/admin/projects/{project_id}/settings", json=fields))

    def delete_project(self, project_id: str, delete_backup: bool = False) -> dict[str, Any]:
        suffix = "?delete_backup=true" if delete_backup else ""
        return self._json(self.http.delete(f"/api/admin/projects/{project_id}{suffix}")) or {}

    def files(self, project_id: str) -> list[dict[str, Any]]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/files"))

    def file(self, project_id: str, file_name: str) -> dict[str, Any]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/files/{file_name}"))

    def upload(
        self, project_id: str, file_name: str, path: Path, mode: str = "replace", summary: str = ""
    ) -> dict[str, Any]:
        with path.open("rb") as handle:
            return self._json(
                self.http.post(
                    f"/api/admin/projects/{project_id}/files/{file_name}/upload",
                    files={"upload": (path.name, handle, "text/markdown")},
                    data={"mode": mode, "compact_summary": summary},
                    timeout=900,
                )
            )

    def llm(self) -> dict[str, Any]:
        return self._json(self.http.get("/api/admin/llm"))

    def save_llm(self, **body: Any) -> dict[str, Any]:
        return self._json(self.http.put("/api/admin/llm", json=body))

    def test_llm(self) -> dict[str, Any]:
        return self._json(self.http.post("/api/admin/llm/test", timeout=120))

    def llm_models(
        self,
        provider: str,
        api_key: str | None = None,
        base_url: str | None = None,
        region: str | None = None,
        auth: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"provider": provider}
        if api_key:
            body["api_key"] = api_key
        if base_url:
            body["base_url"] = base_url
        if region:
            body["region"] = region
        if auth:
            body["auth"] = auth
        return self._json(self.http.post("/api/admin/llm/models", json=body, timeout=90))

    def llm_login(self) -> dict[str, Any]:
        return self._json(self.http.get("/api/admin/llm/login"))

    def oauth_status(self) -> dict[str, Any]:
        return self._json(self.http.get("/api/admin/llm/oauth"))

    def oauth_start(self, provider: str, local_server: bool = True) -> dict[str, Any]:
        return self._json(
            self.http.post(
                "/api/admin/llm/oauth/start",
                json={"provider": provider, "local_server": local_server},
            )
        )

    def oauth_wait(self) -> dict[str, Any]:
        return self._json(self.http.post("/api/admin/llm/oauth/wait", timeout=40))

    def oauth_complete(self, code: str = "", model: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"code": code}
        if model:
            body["model"] = model
        return self._json(self.http.post("/api/admin/llm/oauth/complete", json=body, timeout=180))

    def gemini_project(self, project: str) -> dict[str, Any]:
        return self._json(
            self.http.post(
                "/api/admin/llm/oauth/gemini-project", json={"project": project}, timeout=120
            )
        )

    def oauth_sign_out(self, provider: str) -> dict[str, Any]:
        return self._json(self.http.delete(f"/api/admin/llm/oauth/{provider}"))

    def chat(self, project_id: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        return self._json(
            self.http.post(
                f"/api/admin/projects/{project_id}/chat", json={"messages": messages}, timeout=600
            )
        )

    def chat_apply(self, project_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        return self._json(
            self.http.post(
                f"/api/admin/projects/{project_id}/chat/apply", json={"changes": changes}
            )
        )

    def github(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/github"))

    def connect_github(self, project_id: str, **body: Any) -> dict[str, Any]:
        return self._json(self.http.put(f"/api/admin/projects/{project_id}/github", json=body))

    def disconnect_github(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.delete(f"/api/admin/projects/{project_id}/github"))

    def analyze(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.post(f"/api/admin/projects/{project_id}/github/analyze"))

    def analysis(self, project_id: str) -> dict[str, Any]:
        return self._json(self.http.get(f"/api/admin/projects/{project_id}/github/analysis"))

    def events(self, project_id: str, stop: threading.Event) -> Iterator[dict[str, Any]]:
        with self.http.stream(
            "GET", f"/api/admin/projects/{project_id}/events", timeout=None
        ) as stream:
            for line in stream.iter_lines():
                if stop.is_set():
                    return
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except ValueError:
                        continue

    def close(self) -> None:
        self.http.close()


class Shell:
    def __init__(self, settings: Settings, env_path: Path, open_browser: bool = False) -> None:
        self.settings = settings
        self.env_path = env_path
        self.handle = ServerHandle(settings)
        self.client: AdminClient | None = None
        self.tunnel: subprocess.Popen[str] | None = None
        self.tunnel_url: str | None = None
        self.named_tunnel: str | None = None
        self.open_browser = open_browser
        self.project_id: str | None = None
        self.cwd = Path.cwd()
        self.code_stamp = code_stamp()
        self.code_warned = False
        self.cloudflared = cloudflare.installer(env_path.parent / "data" / "bin")
        self.attached = False
        self.oauth_sign_in_result: dict[str, Any] = {}
        self.handlers: dict[str, Callable[[list[str]], None]] = {
            "help": self.cmd_help,
            "status": self.cmd_status,
            "menu": self.cmd_menu,
            "open": self.cmd_open,
            "theme": self.cmd_theme,
            "clear": self.cmd_clear,
            "projects": self.cmd_projects,
            "new": self.cmd_new,
            "use": self.cmd_use,
            "delete": self.cmd_delete,
            "backup": self.cmd_backup,
            "key": self.cmd_key,
            "keys": self.cmd_keys,
            "agent": self.cmd_agent,
            "revoke": self.cmd_revoke,
            "files": self.cmd_files,
            "show": self.cmd_show,
            "upload": self.cmd_upload,
            "activity": self.cmd_activity,
            "follow": self.cmd_follow,
            "agents": self.cmd_agents,
            "docs": self.cmd_docs,
            "github": self.cmd_github,
            "analyze": self.cmd_analyze,
            "llm": self.cmd_llm,
            "chat": self.cmd_chat,
            "connect": self.cmd_connect,
            "tunnel": self.cmd_tunnel,
            "domain": self.cmd_domain,
            "service": self.cmd_service,
            "security": self.cmd_security,
            "protect": self.cmd_protect,
            "master": self.cmd_master,
            "rotate": self.cmd_rotate,
            "env": self.cmd_env,
            "set": self.cmd_set,
            "edit": self.cmd_edit,
            "restart": self.cmd_restart,
        }

    @property
    def admin_url(self) -> str:
        return f"{self.handle.local_url}{self.settings.admin_path}"

    @property
    def public_base(self) -> str:
        if self.named_tunnel and self.settings.brain_domain:
            return f"https://{self.settings.brain_domain}"
        if self.tunnel_url:
            return self.tunnel_url
        if self.settings.brain_domain:
            return f"https://{self.settings.brain_domain}"
        if re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", self.settings.public_url):
            return self.handle.local_url
        return self.settings.public_url

    @property
    def api(self) -> AdminClient:
        assert self.client is not None
        return self.client

    def run(self) -> None:
        safe_stdio()
        if not self.settings.brain_theme:
            chosen = choose_theme()
            update_env(self.env_path, BRAIN_THEME=chosen)
            reset_settings_cache()
            self.settings = get_settings()
            self.handle.settings = self.settings
        apply_theme(self.settings.brain_theme)
        print_logo(console, current.primary, current.accent, current.muted, __version__)
        if background.healthy(self.handle.local_url):
            self.attached = True
            console.print(
                f"[muted]the brain is already running on port {self.handle.port} as a background "
                "service; this console is attached to it[/muted]"
            )
        else:
            with console.status(
                f"[muted]starting the server on port {self.handle.port}...[/muted]"
            ):
                try:
                    self.handle.start()
                except RuntimeError as error:
                    console.print(f"[danger]{error}[/danger]")
                    raise typer.Exit(code=1) from error
        self.client = AdminClient(self.handle.local_url, self.settings.admin_master_key)
        if self.open_browser:
            webbrowser.open(f"{self.admin_url}?key={self.settings.admin_master_key}")
        if self.settings.cloudflare_tunnel and self.settings.brain_domain:
            if self.attached:
                self.named_tunnel = self.settings.cloudflare_tunnel
            else:
                self.resume_named_tunnel()
        self.status_block()
        console.print(
            "[muted]Type[/muted] [key]/help[/key] [muted]for commands. Shell commands such as[/muted] "
            "[key]ls[/key][muted],[/muted] [key]cat[/key] [muted]and[/muted] [key]grep[/key] "
            "[muted]work here too.[/muted]"
        )
        try:
            while True:
                try:
                    line = self.read_line()
                except KeyboardInterrupt:
                    console.print("\n[muted]type /quit to stop the server and leave[/muted]")
                    continue
                if not self.execute(line):
                    break
        except EOFError:
            console.print()
        finally:
            self.shutdown()

    def read_line(self) -> str:
        if not self.code_warned and code_stamp() != self.code_stamp:
            self.code_warned = True
            console.print(
                "[warning]brain was updated on disk since this console started: type /quit and run "
                "brain again to use the new version[/warning]"
            )
        label = "[primary]brain[/primary]"
        if self.project_id:
            label += f" [muted]@{self.project_id}[/muted]"
        return read_input(f"\n{label}[primary]>[/primary] ").strip()

    def execute(self, line: str) -> bool:
        if not line:
            return True
        if line.startswith("/"):
            tokens = tokenize(line[1:])
            if not tokens:
                return True
            name, args = tokens[0].casefold(), tokens[1:]
        elif line.casefold() in BARE_WORDS:
            name, args = BARE_WORDS[line.casefold()], []
        else:
            self.run_shell(line)
            return True
        command = COMMAND_INDEX.get(name)
        if command is None:
            close = difflib.get_close_matches(name, list(COMMAND_INDEX), n=1)
            hint = f"; did you mean [key]/{close[0]}[/key]?" if close else ""
            console.print(
                f"[warning]unknown command /{name}[/warning]{hint} [muted](/help lists them)[/muted]"
            )
            return True
        if command.name == "quit":
            return False
        try:
            self.handlers[command.name](args)
        except RuntimeError as error:
            console.print(f"[danger]{error}[/danger]")
        except httpx.HTTPError as error:
            console.print(f"[danger]could not reach the server: {error}[/danger]")
        except (KeyboardInterrupt, EOFError):
            console.print()
        return True

    def shutdown(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.attached:
            console.print(
                "[success]bye.[/success] The background service keeps the brain running; "
                "[key]brain service stop[/key] stops it."
            )
            return
        console.print("[muted]stopping the server...[/muted]")
        if self.tunnel is not None:
            self.tunnel.terminate()
            if self.named_tunnel:
                console.print(
                    f"[muted]https://{self.settings.brain_domain} goes offline until the next "
                    "brain start; /service keeps it up next time[/muted]"
                )
        self.handle.stop()
        console.print("[success]stopped.[/success] Run [key]brain[/key] to start again.")

    def reload(self) -> None:
        reset_settings_cache()
        self.settings = get_settings()
        if self.attached:
            with console.status("[muted]restarting the background service...[/muted]"):
                background.restart(self.spec())
        else:
            self.handle.restart(self.settings)
        if self.client is not None:
            self.client.close()
        self.client = AdminClient(self.handle.local_url, self.settings.admin_master_key)

    def spec(self) -> background.Spec:
        directory = self.env_path.parent
        system_spec = background.Spec(directory, self.settings.brain_port, system=True)
        if background.kind() == "systemd" and background.systemd_unit_path(system_spec).is_file():
            return system_spec
        return background.Spec(directory, self.settings.brain_port)

    def projects(self) -> list[dict[str, Any]]:
        try:
            return self.api.projects()
        except (RuntimeError, httpx.HTTPError):
            return []

    def resolve_project(self, args: list[str], options: dict[str, str] | None = None) -> str | None:
        projects = self.projects()
        ids = [p["project_id"] for p in projects]
        wanted = (options or {}).get("project")
        if not wanted:
            for token in args:
                if token in ids:
                    wanted = token
                    break
        if wanted:
            if wanted in ids:
                self.project_id = wanted
                return wanted
            console.print(
                f"[warning]no project {wanted}; projects: {', '.join(ids) or 'none'}[/warning]"
            )
            return None
        if self.project_id in ids:
            return self.project_id
        if len(ids) == 1:
            self.project_id = ids[0]
            return ids[0]
        if not ids:
            console.print(
                "[warning]no projects yet;[/warning] create one with [key]/new <company name>[/key]"
            )
            return None
        picked = choose(
            console,
            "Project",
            [Choice(p["project_id"], p["project_id"], p["project_name"]) for p in projects],
            ask=prompt,
            cancel="Cancel",
        )
        if picked is None:
            return None
        self.project_id = picked.value
        return picked.value

    def status_block(self) -> None:
        projects = self.projects()
        body = grid()
        body.add_row(
            "Admin panel",
            f"[link={self.admin_url}]{self.admin_url}[/link]   [muted]/open[/muted]",
        )
        body.add_row(
            "Folder",
            f"{self.env_path.parent}   [muted].env, data and brain_files live here[/muted]",
        )
        saved_domain = bool(self.settings.brain_domain) and not self.named_tunnel
        domain_up = saved_domain and domain_live(self.settings.brain_domain)
        endpoint = self.handle.local_url if saved_domain and not domain_up else self.public_base
        body.add_row(
            "MCP endpoint",
            f"{endpoint}/mcp"
            + (
                f"   [warning]keys embed https://{self.settings.brain_domain} until it is "
                "connected or forgotten[/warning]"
                if saved_domain and not domain_up
                else ""
            ),
        )
        local_only = self.settings.brain_host in ("127.0.0.1", "localhost")
        if self.named_tunnel:
            running = self.attached or (self.tunnel is not None and self.tunnel.poll() is None)
            if self.attached:
                state = "[success]live through your Cloudflare Tunnel, kept up by the background service[/success]"
            elif running:
                state = (
                    "[success]live through your Cloudflare Tunnel while this console runs; "
                    "/service keeps it up after you close it[/success]"
                )
            else:
                state = "[danger]tunnel stopped; /domain restarts it[/danger]"
            body.add_row(
                "Domain", f"[accent]https://{self.settings.brain_domain}[/accent]   " + state
            )
        elif self.tunnel_url:
            body.add_row(
                "Domain",
                f"[accent]{self.tunnel_url}[/accent]   [muted]temporary public URL from the quick "
                "tunnel; /domain connects a permanent one[/muted]",
            )
        elif domain_up:
            body.add_row(
                "Domain",
                f"[accent]https://{self.settings.brain_domain}[/accent]   [success]live[/success] "
                "[muted]served by the server its DNS points at[/muted]",
            )
        elif saved_domain:
            body.add_row(
                "Domain",
                f"https://{self.settings.brain_domain}   [warning]saved but not connected[/warning] "
                "[muted]/domain deploys it from this computer, /domain vps prints the server steps, "
                "/domain forget removes it[/muted]",
            )
        else:
            body.add_row(
                "Domain",
                "[muted]none yet: agents on this computer use the MCP endpoint above; /tunnel gives "
                "a public URL right now, /domain connects your own (brain.yourcompany.com) for "
                "agents and people elsewhere[/muted]",
            )
        body.add_row(
            "Reachable from",
            "this computer only   [muted]/security to change[/muted]"
            if local_only
            else "this computer and your network   [muted]the internet only through /tunnel, "
            "/domain or a VPS[/muted]",
        )
        body.add_row(
            "Background",
            "[success]running as a service; starts at login or boot[/success]   [muted]/service[/muted]"
            if self.attached
            else "[muted]only while this console is open; /service keeps it running after you "
            "close it[/muted]",
        )
        body.add_row(
            "Master key",
            f"{mask(self.settings.admin_master_key, 'ADMIN_MASTER_KEY')}   [muted]/master[/muted]",
        )
        if projects:
            parts = []
            for p in projects[:4]:
                live = f", [success]{p['live_agents']} live[/success]" if p["live_agents"] else ""
                github = ", GitHub" if p.get("github_connected") else ""
                parts.append(
                    f"[accent]{p['project_id']}[/accent] ({p['key_count']} keys{live}{github})"
                )
            more = f" and {len(projects) - 4} more" if len(projects) > 4 else ""
            body.add_row("Projects", "  ".join(parts) + more + "   [muted]/projects[/muted]")
        else:
            body.add_row(
                "Projects", "[warning]none yet[/warning]   [muted]/new <company name>[/muted]"
            )
        llm = self.settings.llm_provider
        body.add_row(
            "LLM",
            f"[success]{llm}[/success] {self.settings.llm_model or ''}   [muted]/llm[/muted]"
            if self.settings.llm_enabled
            else "[muted]not connected (optional)   /llm[/muted]",
        )
        console.print(panel(body, "running", f"theme {current.name} | {self.env_path}"))

    def cmd_help(self, args: list[str]) -> None:
        if args:
            command = COMMAND_INDEX.get(args[0].lstrip("/").casefold())
            if command is None:
                console.print(f"[warning]no command named {args[0]}[/warning]")
                return
            body = grid()
            body.add_row("Usage", f"[accent]{command.usage}[/accent]")
            body.add_row("Does", command.help)
            if command.aliases:
                body.add_row("Aliases", ", ".join(f"/{a}" for a in command.aliases))
            console.print(panel(body, f"/{command.name}"))
            return
        for group in ("Brain", "Sync", "Server", "Console"):
            table = listing(group, "Command", "What it does")
            for command in COMMANDS:
                if command.group == group:
                    table.add_row(f"[accent]{command.usage}[/accent]", command.help)
            console.print(table)
            console.print()
        console.print(
            "[muted]Anything without a slash runs in your shell:[/muted] [key]ls[/key], [key]cat README.md[/key], "
            "[key]grep -n price brain_files[/key], [key]git status[/key], [key]cd docs[/key]. "
            "[muted]Press Enter alone to repaint the prompt.[/muted]"
        )

    def cmd_status(self, args: list[str]) -> None:
        self.status_block()
        for p in self.projects():
            try:
                github = self.api.github(p["project_id"])
            except RuntimeError:
                continue
            if github["connected"]:
                last = github.get("last_analysis") or {}
                when = (
                    f"last analysed {last.get('finished_at')}"
                    if last.get("finished_at")
                    else "never analysed"
                )
                console.print(
                    f"  [accent]{p['project_id']}[/accent] GitHub {github['repo_url']} [muted]({when})[/muted]"
                )

    def cmd_menu(self, args: list[str]) -> None:
        rows = (
            ("Create a project and its API key", "new"),
            ("Add an API key for another agent", "key"),
            ("See what agents changed", "activity"),
            ("Upload documents for the agents (knowledge base)", "docs"),
            ("Connect the GitHub repository", "github"),
            ("Connect an LLM (optional)", "llm"),
            ("Reach agents elsewhere: domain, tunnel, hosting", "connect"),
            ("Settings in .env", "env"),
            ("Security and admin access", "security"),
        )
        picked = choose(
            console,
            "What do you want to do?",
            [Choice(label, command, f"/{command}") for label, command in rows],
            ask=prompt,
            cancel="Back",
        )
        if picked is not None:
            self.handlers[picked.value]([])

    def cmd_open(self, args: list[str]) -> None:
        webbrowser.open(f"{self.admin_url}?key={self.settings.admin_master_key}")
        console.print(f"opened [accent]{self.admin_url}[/accent] signed in")

    def cmd_theme(self, args: list[str]) -> None:
        chosen = palette_for(args[0]).key if args else choose_theme(self.settings.brain_theme)
        update_env(self.env_path, BRAIN_THEME=chosen)
        reset_settings_cache()
        self.settings = get_settings()
        apply_theme(chosen)
        print_logo(console, current.primary, current.accent, current.muted, __version__)
        console.print(f"[success]theme set to {current.name}[/success]")

    def cmd_clear(self, args: list[str]) -> None:
        console.clear()
        print_logo(console, current.primary, current.accent, current.muted, __version__)

    def cmd_projects(self, args: list[str]) -> None:
        projects = self.projects()
        if not projects:
            console.print(
                "[warning]No projects yet.[/warning] Create one with [key]/new <company name>[/key]."
            )
            return
        table = listing(
            "Projects", "Project", "Name", "Keys", "Live", "Stale", "GitHub", "Last change"
        )
        for p in projects:
            live = p["live_agents"]
            stale = p["agents_stale"]
            table.add_row(
                f"[accent]{p['project_id']}[/accent]",
                p["project_name"],
                str(p["key_count"]),
                f"[success]{live}[/success]" if live else str(live),
                f"[warning]{stale}[/warning]" if stale else str(stale),
                "[success]yes[/success]" if p.get("github_connected") else "[muted]no[/muted]",
                f"[muted]{(p.get('last_mutation_summary') or '')[:48]}[/muted]",
            )
        console.print(table)
        if self.project_id:
            console.print(f"[muted]default project: {self.project_id} (change with /use)[/muted]")

    def cmd_use(self, args: list[str]) -> None:
        if not args:
            console.print("[warning]usage: /use <project>[/warning]")
            return
        ids = [p["project_id"] for p in self.projects()]
        if args[0] not in ids:
            console.print(
                f"[warning]no project {args[0]}; projects: {', '.join(ids) or 'none'}[/warning]"
            )
            return
        self.project_id = args[0]
        console.print(f"[success]using {args[0]}[/success]")

    def show_key_result(self, result: dict[str, Any]) -> None:
        body = grid()
        body.add_row(
            "Project",
            f"{result['project']['project_id']}  [muted]{result['project']['project_name']}[/muted]",
        )
        body.add_row("Agent", f"{result['agent_id']}  [muted]role {result['role']}[/muted]")
        body.add_row("API key", f"[accent]{result['plaintext']}[/accent]")
        body.add_row("MCP endpoint", result["mcp_url"])
        body.add_row("Claude Code", f"[muted]{result['snippets']['claude_code'].strip()}[/muted]")
        hermes = "\n".join(result["snippets"]["hermes"].rstrip().splitlines())
        body.add_row("Hermes", f"[muted]{hermes}[/muted]")
        console.print(panel(body, "API key created", "shown once, copy it now", style="success"))
        guide = grid()
        guide.add_row(
            "Connect", "paste the command above into the agent; nothing else to configure"
        )
        guide.add_row(
            "It then",
            "calls whoami and gets its role, rules, file guide and watch list from the brain",
        )
        guide.add_row(
            "Each turn",
            "get_company_log_delta tells it what other agents and humans changed since last time",
        )
        guide.add_row(
            "Reads",
            "read_brain_section for the facts it needs; list_documents, search_documents and "
            "read_document for your uploaded documents",
        )
        guide.add_row(
            "Writes",
            "update_brain_section after it changes something real, log_agent_activity after work "
            "other departments must know about",
        )
        guide.add_row(
            "Backup",
            "every change lands in the project's Git history and backup repository automatically",
        )
        console.print(panel(guide, "What the agent does once connected"))

    def cmd_new(self, args: list[str]) -> None:
        name = " ".join(args).strip() or prompt("Company or project name (Enter to cancel)")
        if not name:
            return
        repo_url = prompt("Company GitHub repository URL (optional, Enter to skip)")
        backup = False
        account = self.api.github_account()
        if not account["connected"]:
            console.print(
                "[muted]A backup repository <project>_brain on your GitHub keeps every brain file "
                "safe and in sync. It needs a GitHub token with the repo scope "
                "(github.com -> Settings -> Developer settings -> Personal access tokens).[/muted]"
            )
            if confirm("Connect your GitHub account now for the backup repository?", default=True):
                self.github_account_setup()
                account = self.api.github_account()
        if account["connected"]:
            backup = confirm(
                f"Create the private backup repository {slug_hint(name)}_brain on GitHub "
                f"({account['owner']}) and push every change there?",
                default=True,
            )
        with console.status("[muted]creating the project...[/muted]"):
            result = self.api.quick_project(name, repo_url=repo_url or None, backup=backup)
        self.show_key_result(result)
        self.project_id = result["project"]["project_id"]
        if result.get("github"):
            console.print(f"[success]repository {result['github']['repo_url']} connected[/success]")
            secret = result["github"].get("webhook_secret")
            if secret:
                steps = grid()
                steps.add_row(
                    "Payload URL",
                    f"[accent]{self.public_base}/webhooks/{self.project_id}/github[/accent]",
                )
                steps.add_row("Secret", f"[accent]{secret}[/accent]  [muted]shown once[/muted]")
                steps.add_row(
                    "In GitHub",
                    "Settings -> Webhooks -> Add webhook, application/json, event Pushes",
                )
                console.print(panel(steps, "Add the webhook so pushes reach the brain"))
        if result.get("github_error"):
            console.print(f"[warning]repository not connected: {result['github_error']}[/warning]")
        if result.get("backup"):
            console.print(
                f"[success]backup repository {result['backup']['repo']} created;[/success] "
                f"[muted]{result['backup']['html_url']} receives every change[/muted]"
            )
        if result.get("backup_error"):
            console.print(
                f"[warning]backup repository not created: {result['backup_error']}[/warning]"
            )
        while confirm("Add another key to this project now (one per agent)?", default=True):
            purpose = prompt("What is the key for (e.g. Sales Hermes on VPS 1)")
            if not purpose:
                break
            self.show_key_result(self.api.quick_key(purpose, project_id=self.project_id))
        console.print(
            "[muted]Next: /docs add uploads documents for the agents, /llm connects a model, "
            "/analyze fills the brain from the code.[/muted]"
        )

    def github_account_setup(self) -> bool:
        token = prompt("GitHub token (repo scope)", hide=True)
        if not token:
            return False
        try:
            with console.status("[muted]checking the token with GitHub...[/muted]"):
                account = self.api.connect_github_account(token)
        except RuntimeError as error:
            console.print(f"[danger]{error}[/danger]")
            return False
        self.reload()
        console.print(f"[success]GitHub account {account['owner']} connected[/success]")
        return True

    def cmd_backup(self, args: list[str]) -> None:
        positional, options = split_options(args)
        if positional and positional[0].casefold() == "account":
            self.github_account_setup()
            return
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        project = next((p for p in self.projects() if p["project_id"] == pid), None)
        if project and project.get("backup_repo"):
            console.print(
                f"[success]{pid} already backs up to {project['backup_repo']}[/success] "
                f"[muted]{project.get('backup_url')}[/muted]"
            )
            return
        account = self.api.github_account()
        if not account["connected"] and not self.github_account_setup():
            return
        with console.status("[muted]creating the backup repository...[/muted]"):
            result = self.api.create_backup(pid)
        backup = result["backup"]
        console.print(
            f"[success]{backup['repo']} {'reused' if backup.get('reused') else 'created'};[/success] "
            f"[muted]{backup['html_url']} receives every change from now on[/muted]"
        )

    def cmd_delete(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        project = next((p for p in self.projects() if p["project_id"] == pid), None)
        body = grid()
        body.add_row(
            "Project", f"{pid}  [muted]{project['project_name'] if project else ''}[/muted]"
        )
        body.add_row(
            "Removes",
            "the vault with all brain files, every API key, the documents, the ledger and proposals",
        )
        if project and project.get("backup_repo"):
            body.add_row(
                "Backup", f"{project['backup_repo']} on GitHub stays unless you choose to delete it"
            )
        console.print(panel(body, "Delete project", style="danger"))
        if not confirm(f"Step 1 of 2: delete {pid}?", default=False):
            return
        delete_backup = False
        if project and project.get("backup_repo"):
            delete_backup = confirm(
                f"Also delete the backup repository {project['backup_repo']} on GitHub?",
                default=False,
            )
        if prompt(f"Step 2 of 2: type {pid} to confirm") != pid:
            console.print("[muted]not deleted[/muted]")
            return
        outcome = self.api.delete_project(pid, delete_backup)
        if self.project_id == pid:
            self.project_id = None
        console.print(f"[success]{pid} deleted[/success]")
        if outcome.get("backup_deleted"):
            console.print("[success]backup repository deleted[/success]")
        elif outcome.get("backup_error"):
            console.print(f"[warning]backup repository kept: {outcome['backup_error']}[/warning]")

    def cmd_key(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project([], options)
        if pid is None:
            return
        purpose = " ".join(positional).strip() or prompt(
            "What is the key for (e.g. Marketing Hermes on VPS 2)"
        )
        if purpose:
            self.show_key_result(self.api.quick_key(purpose, project_id=pid))

    def key_table(self, pid: str) -> list[dict[str, Any]]:
        keys = self.api.keys(pid)
        table = listing(
            f"{pid}: API keys", "#", "Agent", "Role", "Key prefix", "Status", "Last sync"
        )
        for index, k in enumerate(keys, 1):
            if not k["active"]:
                status = "[muted]revoked[/muted]"
            elif k["stale"]:
                status = "[warning]stale[/warning]"
            else:
                status = "[success]active[/success]"
            table.add_row(
                str(index),
                f"[accent]{k['agent_id']}[/accent]",
                k["department_role"],
                f"[muted]{k['key_prefix']}[/muted]",
                status,
                k.get("last_sync_at") or "[muted]never[/muted]",
            )
        console.print(table)
        return keys

    def cmd_keys(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        keys = self.key_table(pid)
        if not keys:
            console.print("[muted]no keys yet; /key <what it is for> creates one[/muted]")
            return
        console.print(
            "[muted]/agent shows an agent's key and connection commands again and "
            "replaces a lost key, /key adds an agent[/muted]"
        )

    def cmd_agent(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project([], options)
        if pid is None:
            return
        keys = [k for k in self.api.keys(pid) if k["active"]]
        if not keys:
            console.print(
                "[muted]no agents yet; /key <what it is for> creates the first key[/muted]"
            )
            return
        wanted = positional[0].casefold() if positional else ""
        key = next((k for k in keys if k["agent_id"] == wanted), None)
        if key is None:
            picked = choose(
                console,
                "Which agent?",
                [
                    Choice(k["agent_id"], k, f"{k['department_role']}  {k['key_prefix']}")
                    for k in keys
                ],
                ask=prompt,
                cancel="Cancel",
            )
            if picked is None:
                return
            key = picked.value
        info = self.api.connect_info(pid, key["key_id"])
        body = grid()
        body.add_row(
            "Agent", f"[accent]{info['agent_id']}[/accent]  [muted]role {info['role']}[/muted]"
        )
        body.add_row("MCP endpoint", f"[accent]{info['mcp_url']}[/accent]")
        if info.get("plaintext"):
            body.add_row("Key", f"[accent]{info['plaintext']}[/accent]")
        else:
            body.add_row(
                "Key",
                f"{key['key_prefix']}  [muted]created before keys were kept, so it cannot be "
                f"shown; the commands below carry {info['placeholder']} in its place[/muted]",
            )
        body.add_row("Claude Code", info["snippets"]["claude_code"].strip())
        body.add_row("Hermes", "~/.hermes/config.yaml:\n" + info["snippets"]["hermes"].rstrip())
        body.add_row("OpenClaw", info["snippets"]["openclaw"].strip())
        console.print(panel(body, f"Connect {info['agent_id']} to the brain"))
        if not info.get("plaintext") and confirm(
            "Issue a replacement key now so it can be shown here (the old one stops working)?",
            default=True,
        ):
            self.show_key_result(self.api.reissue(pid, key["key_id"]))

    def cmd_revoke(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        keys = [k for k in self.key_table(pid) if k["active"]]
        if not keys:
            return
        picked = choose(
            console,
            "Revoke which key?",
            [Choice(k["agent_id"], k, f"{k['department_role']}  {k['key_prefix']}") for k in keys],
            ask=prompt,
            cancel="Cancel",
        )
        if picked is None:
            return
        key = picked.value
        if confirm(
            f"Revoke the key of {key['agent_id']}? The agent loses access immediately.",
            default=False,
        ):
            self.api.revoke(pid, key["key_id"])
            console.print("[success]revoked[/success]")

    def cmd_files(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        table = listing(f"{pid}: brain files", "File", "Etag", "Sections")
        for f in self.api.files(pid):
            names = ", ".join(s["heading"] for s in f["sections"] if s.get("level") == 2)
            table.add_row(
                f"[accent]{f['file']}[/accent]", f"[muted]{f['etag']}[/muted]", names[:90]
            )
        console.print(table)
        console.print(
            "[muted]/show FILE.md prints one; /upload <path> replaces one from disk[/muted]"
        )

    def cmd_show(self, args: list[str]) -> None:
        positional, options = split_options(args)
        if not positional:
            console.print("[warning]usage: /show <FILE.md> [project][/warning]")
            return
        target = guess_file(positional[0])
        if target is None:
            console.print(f"[warning]choose one of: {', '.join(CANONICAL_FILES)}[/warning]")
            return
        pid = self.resolve_project(positional[1:], options)
        if pid is None:
            return
        doc = self.api.file(pid, target)
        console.print(
            panel(markdown_lines(doc["content"]), f"{pid}/{target}", f"etag {doc['etag']}")
        )

    def cmd_upload(self, args: list[str]) -> None:
        positional, options = split_options(args)
        if not positional:
            console.print(
                "[warning]usage: /upload <path> [FILE.md] [-p project] [--smart][/warning]"
            )
            return
        path = (self.cwd / positional[0]).resolve()
        if not path.is_file():
            console.print(f"[danger]{path} is not a file[/danger]")
            return
        target = guess_file(positional[1]) if len(positional) > 1 else None
        if len(positional) > 1 and target is None:
            console.print(f"[warning]choose one of: {', '.join(CANONICAL_FILES)}[/warning]")
            return
        pid = self.resolve_project(positional[2:], options)
        if pid is None:
            return
        if target is None:
            target = guess_file(path.name)
            if target is None:
                picked = choose(
                    console,
                    "Which brain file should it become?",
                    [Choice(name, name) for name in CANONICAL_FILES],
                    ask=prompt,
                    cancel="Cancel",
                )
                target = picked.value if picked else None
            if target is None:
                console.print("[warning]cancelled[/warning]")
                return
        mode = "smart" if "smart" in options else "replace"
        if mode == "smart" and not self.settings.llm_enabled:
            console.print(
                "[warning]--smart needs an LLM; run /llm first. Uploading with replace instead.[/warning]"
            )
            mode = "replace"
        verb = "merge into" if mode == "smart" else "replace"
        if not confirm(f"{verb.capitalize()} {target} in {pid} with {path.name}?", default=True):
            return
        with console.status(
            "[muted]uploading...[/muted]"
            if mode == "replace"
            else "[muted]merging with the LLM...[/muted]"
        ):
            result = self.api.upload(pid, target, path, mode)
        console.print(
            f"[success]{target} updated[/success] [muted](etag {result.get('new_etag') or result.get('file_etag')}, "
            f"{result.get('bytes')} bytes, mode {result.get('mode')}); agents see it on their next sync[/muted]"
        )

    def cmd_activity(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        entries = self.api.recent(pid, 20)
        table = listing(f"{pid}: latest changes", "Time", "Actor", "Target", "Summary")
        for e in entries:
            table.add_row(
                f"[muted]{(e.get('created_at') or '')[11:16]}[/muted]",
                f"[accent]{e['actor']}[/accent]",
                e["target"].split("/", 3)[-1][:40],
                e["summary"][:70],
            )
        console.print(table)
        live = self.api.sessions(pid)
        names = ", ".join(s["agent_id"] for s in live)
        console.print(
            f"connected now: [success]{names}[/success]"
            if names
            else "connected now: [muted]nobody[/muted]"
        )
        console.print("[muted]/follow streams changes live[/muted]")

    def cmd_follow(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        console.print(f"[muted]following {pid}; press Enter to stop[/muted]")
        stop = threading.Event()

        def follow() -> None:
            try:
                for event in self.api.events(pid, stop):
                    stamp = (event.get("ts") or "")[11:19]
                    actor = f"{event.get('department') or ''}/{event.get('agent_id') or ''}"
                    console.print(
                        f"[muted]{stamp}[/muted] [primary]{event.get('type')}[/primary] "
                        f"[accent]{actor}[/accent] {event.get('summary') or ''}"
                    )
            except Exception:
                return

        threading.Thread(target=follow, daemon=True).start()
        try:
            input()
        finally:
            stop.set()

    def cmd_agents(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        live = self.api.sessions(pid)
        table = listing(
            f"{pid}: connected now", "Agent", "Role", "Transport", "Client", "Last seen"
        )
        for s in live:
            table.add_row(
                f"[accent]{s['agent_id']}[/accent]",
                s["department_role"],
                s["transport"],
                s.get("client_info") or "",
                (s.get("last_seen_at") or "")[11:19],
            )
        console.print(table)
        if not live:
            console.print("[muted]nobody is connected right now[/muted]")
        self.key_table(pid)

    def document_table(self, pid: str) -> list[dict[str, Any]]:
        documents = self.api.documents(pid)
        table = listing(
            f"{pid}: documents", "#", "Title", "File", "Size", "Shared with", "Uploaded"
        )
        for index, d in enumerate(documents, 1):
            shared = (
                "[success]all agents[/success]" if d["shared_with_all"] else ", ".join(d["access"])
            )
            table.add_row(
                str(index),
                f"[accent]{d['title']}[/accent]",
                f"[muted]{d['file_name']}[/muted]",
                f"{d['text_chars'] // 1000}k chars",
                shared,
                (d.get("created_at") or "")[:10],
            )
        console.print(table)
        if not documents:
            console.print("[muted]no documents yet; /docs add <path> uploads one[/muted]")
        return documents

    def pick_document(
        self, documents: list[dict[str, Any]], args: list[str]
    ) -> dict[str, Any] | None:
        if args:
            token = args[0]
            if token.isdigit() and 0 < int(token) <= len(documents):
                return documents[int(token) - 1]
            match = next((d for d in documents if d["doc_id"] == token), None)
            if match is None:
                console.print(f"[warning]no document {token}[/warning]")
            return match
        picked = choose(
            console,
            "Document",
            [Choice(d["title"], d, d["original_name"]) for d in documents],
            ask=prompt,
            cancel="Cancel",
        )
        return picked.value if picked else None

    def cmd_docs(self, args: list[str]) -> None:
        positional, options = split_options(args)
        action = positional[0].casefold() if positional else "list"
        rest = positional[1:]
        if action == "add":
            if not rest:
                console.print(
                    "[warning]usage: /docs add <path> [--title t] [--agents a,b] [--roles r1,r2] [-p project][/warning]"
                )
                return
            path = (self.cwd / rest[0]).resolve()
            if not path.is_file():
                console.print(f"[danger]{path} is not a file[/danger]")
                return
            pid = self.resolve_project(rest[1:], options)
            if pid is None:
                return
            access = "*"
            if options.get("agents") and options["agents"] != "true":
                access = options["agents"]
            if options.get("roles") and options["roles"] != "true":
                roles = ",".join(
                    f"role:{r.strip()}" for r in options["roles"].split(",") if r.strip()
                )
                access = roles if access == "*" else f"{access},{roles}"
            if access == "*" and "agents" not in options and "roles" not in options:
                keys = [k for k in self.api.keys(pid) if k["active"]]
                if keys and not confirm("Share it with every agent of the project?", default=True):
                    console.print(
                        "Agents: " + ", ".join(f"[accent]{k['agent_id']}[/accent]" for k in keys)
                    )
                    access = prompt(
                        "Agent IDs, comma separated (role:sales also works)",
                        default=keys[0]["agent_id"],
                    )
            title = options.get("title") if options.get("title") not in (None, "true") else ""
            with console.status("[muted]uploading and extracting text...[/muted]"):
                document = self.api.add_document(pid, path, title or "", access)
            console.print(
                f"[success]{document['title']} added[/success] [muted]({document['text_chars']} characters, "
                f"shared with {'all agents' if document['shared_with_all'] else ', '.join(document['access'])}); "
                "agents see it with list_documents[/muted]"
            )
            return
        pid = self.resolve_project(rest if action != "list" else positional, options)
        if pid is None:
            return
        documents = self.document_table(pid)
        if action == "list" or not documents:
            if documents:
                console.print(
                    "[muted]/docs add <path> uploads; /docs share, /docs read, /docs rm act on a number[/muted]"
                )
            return
        if action in ("rm", "remove", "delete"):
            chosen = self.pick_document(documents, rest)
            if chosen and confirm(f"Delete '{chosen['title']}'?", default=False):
                self.api.delete_document(pid, chosen["doc_id"])
                console.print("[success]deleted[/success]")
        elif action in ("share", "access"):
            chosen = self.pick_document(documents, rest)
            if chosen is None:
                return
            keys = [k for k in self.api.keys(pid) if k["active"]]
            console.print(
                "Agents: "
                + (
                    ", ".join(f"[accent]{k['agent_id']}[/accent]" for k in keys)
                    or "[muted]none[/muted]"
                )
            )
            answer = prompt(
                "Share with (all, or agent IDs / role:name comma separated)", default="all"
            )
            updated = self.api.update_document(pid, chosen["doc_id"], access=answer)
            console.print(
                f"[success]now shared with {'all agents' if updated['shared_with_all'] else ', '.join(updated['access'])}[/success]"
            )
        elif action in ("read", "show", "view"):
            chosen = self.pick_document(documents, rest)
            if chosen is None:
                return
            detail = self.api.document(pid, chosen["doc_id"])
            console.print(
                panel(
                    Text(detail["preview"]),
                    detail["title"],
                    f"first {len(detail['preview'])} of {detail['text_chars']} characters",
                )
            )
        else:
            console.print("[warning]usage: /docs [add|rm|share|read][/warning]")

    def cmd_github(self, args: list[str]) -> None:
        positional, options = split_options(args)
        if positional and positional[0].casefold() in ("off", "disconnect", "remove"):
            pid = self.resolve_project(positional[1:], options)
            if pid is None:
                return
            self.api.disconnect_github(pid)
            console.print(f"[success]{pid} no longer has a repository[/success]")
            return
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        state = self.api.github(pid)
        body = grid()
        body.add_row("Repository", state["repo_url"] or "[muted]not connected[/muted]")
        if state["connected"]:
            body.add_row("Branch", state["branch"] or "[muted]default[/muted]")
            body.add_row(
                "Access token",
                "[success]set[/success]"
                if state["token_set"]
                else "[muted]none (public repository)[/muted]",
            )
            body.add_row(
                "Analyse on push",
                "[success]yes[/success]" if state["analyze_on_push"] else "[muted]no[/muted]",
            )
            last = state.get("last_analysis") or {}
            if last.get("finished_at"):
                outcome = (
                    f"[success]ok[/success] {last.get('sections_updated')} sections, {last.get('findings')} findings"
                    if last.get("ok")
                    else f"[danger]failed: {last.get('error')}[/danger]"
                )
                body.add_row("Last analysis", f"{last['finished_at']}  {outcome}")
        body.add_row(
            "Webhook",
            f"{state['webhook_url']}  "
            + (
                "[success]secret set[/success]"
                if state["webhook_configured"]
                else "[muted]no secret yet[/muted]"
            ),
        )
        body.add_row(
            "LLM",
            "[success]connected[/success]"
            if state["llm_enabled"]
            else "[muted]not connected; /llm (optional)[/muted]",
        )
        console.print(panel(body, f"{pid}: GitHub"))
        console.print(
            "[muted]Connecting a repository lets the brain learn the design system, stack, schema and product facts "
            "from the code (with an LLM) and logs every push in the company log.[/muted]"
        )
        if not confirm("Connect or update the repository now?", default=not state["connected"]):
            return
        repo_url = prompt("Repository URL (https://github.com/org/repo)", default=state["repo_url"])
        if not repo_url:
            return
        branch = prompt("Branch (empty = default branch)", default=state["branch"])
        token = prompt(
            "Access token for a private repository (empty keeps the current one)", hide=True
        )
        analyze = confirm("Analyse the repository on every push?", default=state["analyze_on_push"])
        body_args: dict[str, Any] = {
            "repo_url": repo_url,
            "branch": branch,
            "analyze_on_push": analyze,
        }
        if token:
            body_args["token"] = token
        result = self.api.connect_github(pid, **body_args)
        console.print(f"[success]repository saved for {pid}[/success]")
        steps = grid()
        steps.add_row("Payload URL", f"[accent]{result['webhook_url']}[/accent]")
        if result.get("webhook_secret"):
            steps.add_row(
                "Secret", f"[accent]{result['webhook_secret']}[/accent]  [muted]shown once[/muted]"
            )
        else:
            steps.add_row(
                "Secret",
                "[muted]already set; /keys -> webhook in the admin panel rotates it[/muted]",
            )
        steps.add_row(
            "In GitHub",
            "Settings -> Webhooks -> Add webhook, content type application/json, event Pushes",
        )
        console.print(panel(steps, "Add the webhook so pushes reach the brain"))
        if result["llm_enabled"]:
            if confirm("Analyse the repository now?", default=True):
                self.cmd_analyze([pid])
        else:
            console.print(
                "[muted]Run /llm to connect a model, then /analyze fills the brain from the code.[/muted]"
            )

    def cmd_analyze(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project(positional, options)
        if pid is None:
            return
        started = self.api.analyze(pid)
        if not started["started"]:
            console.print("[warning]an analysis is already running; showing its progress[/warning]")
        self.watch_analysis(pid)

    def watch_analysis(self, pid: str) -> None:
        seen = 0
        deadline = time.time() + 1800
        console.print("[muted]analysing; Ctrl+C stops watching, the analysis continues[/muted]")
        try:
            while time.time() < deadline:
                state = self.api.analysis(pid)
                for step in state.get("steps", [])[seen:]:
                    console.print(f"  [muted]{step['at'][11:19]}[/muted] {step['text']}")
                seen = len(state.get("steps", []))
                if state.get("state") == "done":
                    result = state.get("result") or {}
                    body = grid()
                    body.add_row(
                        "Sections",
                        ", ".join(result.get("sections") or []) or "[muted]none changed[/muted]",
                    )
                    body.add_row("Findings", str(result.get("findings", 0)))
                    body.add_row("Tables", str(result.get("tables", 0)))
                    body.add_row("Model", result.get("model") or "")
                    if result.get("summary"):
                        body.add_row("Summary", result["summary"])
                    console.print(panel(body, "Analysis complete", style="success"))
                    console.print(
                        "[muted]/show TECH_AND_INFRA.md or /activity to see what changed[/muted]"
                    )
                    return
                if state.get("state") == "failed":
                    console.print(f"[danger]analysis failed: {state.get('error')}[/danger]")
                    return
                time.sleep(1)
        except KeyboardInterrupt:
            console.print(
                "\n[muted]still running in the background; /github shows the result later[/muted]"
            )

    def cmd_llm(self, args: list[str]) -> None:
        if args and args[0].casefold() == "test":
            self.test_llm()
            return
        if args and args[0].casefold() in ("off", "disconnect", "remove"):
            self.api.save_llm(provider="")
            self.reload()
            console.print("[success]LLM disconnected[/success]")
            return
        state = self.api.llm()
        body = grid()
        body.add_row(
            "Provider",
            f"[success]{state['provider_label']}[/success]"
            if state["enabled"]
            else "[muted]none[/muted]",
        )
        if state["enabled"]:
            body.add_row(
                "Model", f"{state['model']}  [muted]effort {state.get('effort', 'default')}[/muted]"
            )
            body.add_row("Endpoint", state["base_url"])
            body.add_row(
                "Credentials",
                "Anthropic CLI profile (no API key)"
                if state["auth"] == "login"
                else (
                    "signed-in account (no API key)"
                    if state["auth"] == "oauth"
                    else f"API key {state['key_hint']}"
                ),
            )
        console.print(panel(body, "LLM (optional)"))
        console.print(
            "[muted]With a model connected the brain can analyse your GitHub repository, merge "
            "uploaded documents into the right sections and answer questions in /chat. Agents never "
            "need it: they bring their own model.[/muted]"
        )
        providers = state["providers"]
        choices = [Choice(p["label"], p["key"], p.get("note") or p["key_hint"]) for p in providers]
        choices.append(Choice("None", "", "disconnect the LLM"))
        default = next((i for i, p in enumerate(providers) if p["key"] == state["provider"]), 0)
        picked = choose(console, "LLM provider", choices, default, ask=prompt, cancel="Keep as is")
        if picked is None:
            return
        if picked.value == "":
            self.api.save_llm(provider="")
            self.reload()
            console.print("[success]LLM disconnected[/success]")
            return
        spec = next(p for p in providers if p["key"] == picked.value)
        same = spec["key"] == state["provider"]
        auth = "api_key"
        if spec.get("oauth"):
            account = self.api.oauth_status()["providers"].get(spec["key"], {})
            methods = [
                Choice(
                    f"Sign in with your {account.get('account_label') or spec['label'] + ' account'}",
                    "oauth",
                    account.get("products") or "browser sign-in, no API key to manage",
                ),
                Choice("API key", "api_key", f"{spec['key_hint']} from the developer console"),
            ]
            if spec["key"] == "anthropic":
                methods.insert(
                    1, Choice("Anthropic CLI profile", "login", "ant auth login on this machine")
                )
            current_auth = state["auth"] if same else "oauth"
            method = choose(
                console,
                f"How do you want to connect to {spec['label']}?",
                methods,
                next((i for i, m in enumerate(methods) if m.value == current_auth), 0),
                ask=prompt,
                cancel="Back",
            )
            if method is None:
                return
            auth = method.value
            if auth == "login" and not self.anthropic_sign_in():
                return
            if auth == "oauth":
                if not self.account_sign_in(spec["key"], account):
                    return
                models = self.oauth_sign_in_result["models"]
                state = self.oauth_sign_in_result
                self.pick_model_and_finish(spec, state, models, auth, "", "")
                return
        region = ""
        base_url = spec["base_url"]
        if spec["needs_region"]:
            region = prompt("AWS region", default="us-east-1")
        elif spec["needs_base_url"]:
            base_url = prompt("API base URL", default=state["base_url"] if same else "")
            if not base_url:
                console.print("[warning]cancelled[/warning]")
                return
        api_key = ""
        if auth == "api_key":
            hint = " (Enter keeps the saved key)" if same and state["key_set"] else ""
            api_key = prompt(f"API key {spec['key_hint']}{hint}", hide=True)
            if not api_key and not (same and state["key_set"]) and spec["key"] != "custom":
                console.print("[warning]an API key is required[/warning]")
                return
        with console.status("[muted]checking the credentials and loading the models...[/muted]"):
            try:
                listing = self.api.llm_models(
                    spec["key"], api_key or None, base_url or None, region or None, auth
                )
                models = listing["models"]
                base_url = listing["base_url"]
                error = None
            except RuntimeError as failure:
                models, error = [], str(failure)
        if error:
            console.print(f"[danger]{error}[/danger]")
            if not confirm("Type the model id by hand and save anyway?", default=False):
                return
            model = prompt("Model id", default=state["model"] if same else spec["default_model"])
            self.save_llm_choice(spec, model, base_url, auth, api_key)
            return
        console.print(f"[success]credentials accepted; {len(models)} models available[/success]")
        self.pick_model_and_finish(spec, state, models, auth, base_url, api_key)

    def pick_model_and_finish(
        self,
        spec: dict[str, Any],
        state: dict[str, Any],
        models: list[dict[str, Any]],
        auth: str,
        base_url: str,
        api_key: str,
    ) -> None:
        same = spec["key"] == state.get("provider")
        current_model = state.get("model") if same and state.get("model") else spec["default_model"]
        model_choices = [Choice(m["id"], m["id"], m.get("name") or "") for m in models]
        model_choices.append(Choice("Type another model id", "__custom__"))
        default_model = next((i for i, m in enumerate(models) if m["id"] == current_model), 0)
        chosen = choose(console, "Model", model_choices, default_model, ask=prompt)
        if chosen is None:
            return
        model = (
            prompt("Model id", default=current_model or "")
            if chosen.value == "__custom__"
            else chosen.value
        )
        if not model:
            console.print("[warning]cancelled[/warning]")
            return
        self.save_llm_choice(spec, model, base_url, auth, api_key)

    def pick_effort(self, spec: dict[str, Any], current: str) -> str:
        options = (
            ["low", "medium", "high", "xhigh", "max"]
            if spec["key"] == "anthropic"
            else ["low", "medium", "high"]
        )
        hints = {
            "default": "the provider's own default",
            "low": "fastest and cheapest; quick lookups",
            "medium": "balanced",
            "high": "thorough; analysis of repositories and documents",
            "xhigh": "deeper still; long agentic work",
            "max": "maximum reasoning; slowest and most expensive",
        }
        choices = [Choice(value, value, hints[value]) for value in ["default", *options]]
        default_index = next(
            (i for i, c in enumerate(choices) if c.value == (current or "default")), 0
        )
        picked = choose(console, "Thinking effort", choices, default_index, ask=prompt)
        return "" if picked is None or picked.value == "default" else picked.value

    def save_llm_choice(
        self, spec: dict[str, Any], model: str, base_url: str, auth: str, api_key: str
    ) -> None:
        current = self.settings.llm_effort if spec["key"] == self.settings.llm_provider else ""
        effort = self.pick_effort(spec, current)
        payload: dict[str, Any] = {
            "provider": spec["key"],
            "model": model,
            "base_url": base_url,
            "auth": auth,
            "effort": effort,
        }
        if api_key:
            payload["api_key"] = api_key
        saved = self.api.save_llm(**payload)
        self.reload()
        if not saved["enabled"]:
            console.print("[warning]saved, but the credentials are still missing[/warning]")
            return
        console.print(
            f"[success]{saved['provider_label']} connected with {saved['model']}, effort "
            f"{saved.get('effort', 'default')}[/success] "
            "[muted](/chat to ask the brain, /github then /analyze to fill it from your code)[/muted]"
        )
        if confirm("Send a test message now?", default=True):
            self.test_llm()

    def account_sign_in(self, provider: str, account: dict[str, Any]) -> bool:
        self.oauth_sign_in_result: dict[str, Any] = {}
        if account.get("signed_in") and confirm(
            f"Already signed in as {account.get('account') or 'this account'}. Use that sign-in?",
            default=True,
        ):
            try:
                listing = self.api.llm_models(provider, None, None, None, "oauth")
            except RuntimeError as error:
                console.print(f"[warning]{error}; signing in again[/warning]")
            else:
                self.oauth_sign_in_result = {
                    "models": listing["models"],
                    "provider": provider,
                    "model": "",
                }
                return True
        local = confirm(
            "Is the browser on this machine? (No: you paste the code or address back)", default=True
        )
        try:
            started = self.api.oauth_start(provider, local_server=local)
        except RuntimeError as error:
            console.print(f"[danger]{error}[/danger]")
            return False
        console.print(f"[muted]{started.get('paste_hint') or ''}[/muted]")
        console.print(f"Open this link to sign in:\n[accent]{started['url']}[/accent]")
        if local:
            webbrowser.open(started["url"])
        pasted = ""
        if started.get("listening"):
            with console.status(
                "[muted]waiting for the browser to come back... (Enter to paste instead)[/muted]"
            ):
                for _ in range(8):
                    waited = self.api.oauth_wait()
                    if waited.get("received"):
                        pasted = waited.get("value") or ""
                        break
        if not pasted:
            pasted = prompt("Paste the code or the address the browser was sent to")
            if not pasted:
                console.print("[warning]cancelled[/warning]")
                return False
        try:
            with console.status("[muted]finishing the sign-in and loading the models...[/muted]"):
                result = self.api.oauth_complete(pasted, None)
        except RuntimeError as error:
            console.print(f"[danger]{error}[/danger]")
            return False
        account_name = (result.get("account") or {}).get("account") or ""
        console.print(
            f"[success]signed in{' as ' + account_name if account_name else ''}[/success]"
        )
        if provider == "gemini" and (result.get("account") or {}).get("needs_project"):
            if not self.gemini_project_setup(result["account"]):
                return False
        self.oauth_sign_in_result = {
            "models": result.get("models", []),
            "provider": provider,
            "model": result.get("model", ""),
        }
        return True

    def gemini_project_setup(self, account: dict[str, Any]) -> bool:
        body = grid()
        body.add_row(
            "What Google said",
            account.get("note") or "the free tier is not available to this sign-in",
        )
        body.add_row(
            "Meaning",
            "Google now reserves the free Gemini sign-in for its own apps. Your sign-in works, but "
            "Gemini answers only through a Google Cloud project that has a Gemini Code Assist "
            "licence, or through an API key.",
        )
        body.add_row(
            "Option A",
            "Enter the Google Cloud project ID with the licence (console.cloud.google.com, top bar).",
        )
        body.add_row(
            "Option B",
            "Press Enter, then choose API key: a free key takes a minute at "
            "https://aistudio.google.com/apikey",
        )
        console.print(panel(body, "Gemini needs one more thing", style="warning"))
        project = prompt("Google Cloud project ID (Enter to use an API key instead)")
        if not project:
            return False
        try:
            with console.status(
                "[muted]registering the project with Google and sending a test message...[/muted]"
            ):
                outcome = self.api.gemini_project(project)
        except RuntimeError as error:
            console.print(f"[danger]{error}[/danger]")
            console.print("[muted]choose API key in /llm to use Gemini anyway[/muted]")
            return False
        console.print(
            f"[success]project accepted; Gemini answered in {outcome['check']['latency_ms']} ms[/success]"
        )
        return True

    def anthropic_sign_in(self) -> bool:
        state = self.api.llm_login()
        if state.get("active") and state.get("profile"):
            console.print(f"[muted]{state['profile'].splitlines()[0]}[/muted]")
            if not confirm("Already signed in. Sign in again?", default=False):
                return True
        if not state["available"]:
            steps = grid()
            for index, line in enumerate(state["install"], 1):
                steps.add_row(str(index), line)
            steps.add_row("Then", "run /llm again")
            console.print(
                panel(steps, "The Anthropic CLI (ant) is needed for sign-in", style="warning")
            )
            return False
        binary = shutil.which("ant") or "ant"
        headless = not confirm(
            "Open the browser on this machine? (No prints a link to open elsewhere)", default=True
        )
        command = [binary, "auth", "login"] + (["--no-browser"] if headless else [])
        console.print(
            "[muted]follow the sign-in in your browser; this console continues when it is done[/muted]"
        )
        code = subprocess.call(command)
        if code != 0:
            console.print(f"[danger]sign-in did not complete (exit code {code})[/danger]")
            return False
        console.print("[success]signed in[/success]")
        return True

    def cmd_chat(self, args: list[str]) -> None:
        positional, options = split_options(args)
        pid = self.resolve_project([], options)
        if pid is None:
            return
        if not self.settings.llm_enabled:
            console.print("[warning]connect an LLM first with /llm[/warning]")
            return
        console.print(
            f"[muted]Ask anything about {pid}, or tell the assistant what to change in a brain file. "
            "Type /exit to leave.[/muted]"
        )
        messages: list[dict[str, str]] = []
        pending = " ".join(positional).strip()
        while True:
            text = pending or read_input("[accent]you[/accent][muted]>[/muted] ").strip()
            pending = ""
            if not text:
                continue
            if text.casefold() in ("/exit", "/quit", "exit", "quit", "/back"):
                return
            messages.append({"role": "user", "content": text})
            with console.status("[muted]thinking...[/muted]"):
                try:
                    result = self.api.chat(pid, messages)
                except RuntimeError as error:
                    console.print(f"[danger]{error}[/danger]")
                    messages.pop()
                    continue
            messages.append({"role": "assistant", "content": result["reply"]})
            console.print(panel(markdown_lines(result["reply"]), "assistant", result.get("model")))
            changes = result.get("changes") or []
            if not changes:
                continue
            table = listing("Proposed changes", "#", "File", "Heading", "Mode", "Summary")
            for index, change in enumerate(changes, 1):
                table.add_row(
                    str(index),
                    f"[accent]{change['file']}[/accent]",
                    change["heading"],
                    change["mode"],
                    change["summary"][:70],
                )
            console.print(table)
            if confirm(f"Apply {len(changes)} change(s) to the brain?", default=True):
                outcome = self.api.chat_apply(pid, changes)
                for item in outcome["results"]:
                    mark = (
                        "[success]ok[/success]"
                        if item["ok"]
                        else f"[danger]{item.get('error')}[/danger]"
                    )
                    console.print(f"  {item['file']}#{item['heading']}: {mark}")
                console.print(
                    f"[success]{outcome['applied']} change(s) applied and broadcast to the agents[/success]"
                )

    def test_llm(self) -> None:
        with console.status("[muted]asking the model to say OK...[/muted]"):
            result = self.api.test_llm()
        console.print(
            f"[success]{result['provider']} answered in {result['latency_ms']} ms[/success] "
            f"[muted]({result['model']}: {result['reply']})[/muted]"
        )

    def cmd_connect(self, args: list[str]) -> None:
        picked = choose(
            console,
            "Reach the brain from other machines",
            [
                Choice(
                    "Your own domain from this computer",
                    "domain",
                    "one-command Cloudflare deploy, HTTPS included",
                ),
                Choice("Cloudflare quick tunnel", "tunnel", "temporary public HTTPS URL, now"),
                Choice("Your own domain on a VPS", "vps", "automatic HTTPS through Caddy"),
                Choice("Railway, Render or Fly.io", "hosting", "managed container hosting"),
                Choice("Protect the admin panel", "protect", "unguessable path, IP allowlist"),
            ],
            ask=prompt,
            cancel="Back",
        )
        if picked is None:
            return
        if picked.value == "tunnel":
            self.cmd_tunnel([])
        elif picked.value == "domain":
            self.cmd_domain([])
        elif picked.value == "vps":
            self.cmd_domain(["vps"])
        elif picked.value == "protect":
            self.cmd_protect([])
        else:
            steps = grid()
            steps.add_row("1", "Deploy this repository's Dockerfile on the platform.")
            steps.add_row("2", "Attach a persistent volume at /data and /app/brain_files.")
            steps.add_row(
                "3",
                "Set ADMIN_MASTER_KEY and PUBLIC_URL (the platform URL, or your domain) in its environment.",
            )
            steps.add_row(
                "4", "Open <PUBLIC_URL>/admin, sign in with the master key, create your project."
            )
            steps.add_row(
                "5",
                "Custom domain: add it in the platform, then a CNAME in Cloudflare DNS pointing at the target it shows.",
            )
            steps.add_row(
                "Docs", "[muted]docs/admin-panel-hosting.md, docs/cloudflare-domain.md[/muted]"
            )
            console.print(panel(steps, "Railway, Render, Fly.io"))

    def cmd_tunnel(self, args: list[str]) -> None:
        if self.tunnel is not None and self.tunnel_url:
            console.print(f"tunnel already running: [accent]{self.tunnel_url}[/accent]")
            return
        binary = self.ensure_cloudflared(then="/tunnel again")
        if binary is None:
            return
        process = subprocess.Popen(
            [binary, "tunnel", "--url", self.handle.local_url, "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        url: str | None = None
        deadline = time.time() + 40
        assert process.stdout is not None
        with console.status("[muted]starting the tunnel...[/muted]"):
            while time.time() < deadline and url is None:
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                match = TUNNEL_URL_RE.search(line)
                if match:
                    url = match.group(0)
        if url is None:
            process.terminate()
            console.print(
                "[danger]the tunnel did not report a URL; try again or check your network[/danger]"
            )
            return
        self.tunnel, self.tunnel_url = process, url
        update_env(self.env_path, PUBLIC_URL=url)
        self.reload()
        body = grid()
        body.add_row("Public URL", f"[accent]{url}[/accent]")
        body.add_row("MCP endpoint", f"{url}/mcp")
        body.add_row(
            "Note",
            "keys you create now embed this URL; it changes when the tunnel restarts. /domain gives a permanent one.",
        )
        body.add_row(
            "Security",
            "the admin panel is reachable there too, behind the master key; /protect locks it down.",
        )
        console.print(panel(body, "Tunnel ready", style="success"))

    def cmd_domain(self, args: list[str]) -> None:
        mode = (
            args[0].casefold()
            if args and args[0].casefold() in ("vps", "hosting", "server", "forget", "remove")
            else ""
        )
        given = args[0] if args and not mode else (args[1] if mode and len(args) > 1 else "")
        if mode in ("forget", "remove"):
            self.forget_domain()
            return
        if mode:
            raw = given or prompt(
                "Domain or subdomain for the brain (e.g. brain.yourcompany.com)",
                default=self.settings.brain_domain,
            )
            try:
                domain = cloudflare.valid_hostname(raw)
            except cloudflare.TunnelError as error:
                console.print(f"[danger]{error}[/danger]")
                return
            self.print_server_steps(domain, "vps" if mode in ("vps", "server") else "hosting")
            return
        console.print(
            "[primary]Own domain through Cloudflare.[/primary] Free, HTTPS included, nothing to "
            "open on your router. You need a free Cloudflare account that manages the domain's "
            "DNS; the console creates the tunnel and the DNS record for you. "
            "[muted](/domain vps or /domain hosting for a server instead)[/muted]"
        )
        self.cloudflared.start()
        while True:
            domain = self.ask_domain_name(given)
            if domain is None:
                return
            outcome = self.verify_on_cloudflare(domain)
            if outcome == "ok":
                break
            if outcome != "rename":
                return
            given = ""
        self.print_record_plan(domain)
        if not confirm(f"Deploy https://{domain} now?", default=True):
            console.print("[muted]nothing changed[/muted]")
            return
        self.connect_domain_here(domain)

    def forget_domain(self) -> None:
        domain = self.settings.brain_domain
        if not domain:
            console.print("[muted]no domain is saved[/muted]")
            return
        if not confirm(
            f"Forget https://{domain}? The MCP endpoint goes back to this computer and the "
            "tunnel, if any, stops.",
            default=True,
        ):
            console.print("[muted]nothing changed[/muted]")
            return
        if self.tunnel is not None and self.named_tunnel:
            self.tunnel.terminate()
            self.tunnel = None
        self.named_tunnel = None
        update_env(
            self.env_path,
            BRAIN_DOMAIN="",
            PUBLIC_URL=f"http://localhost:{self.settings.brain_port}",
            CLOUDFLARE_TUNNEL="",
        )
        self.reload()
        console.print(
            f"[success]forgot {domain}[/success] [muted]the MCP endpoint is {self.handle.local_url}/mcp "
            "again; /domain connects a domain when you are ready[/muted]"
        )

    def print_record_plan(self, domain: str) -> None:
        root = ".".join(domain.split(".")[-2:])
        label = domain[: -len(root) - 1] if domain.endswith("." + root) and domain != root else "@"
        name = cloudflare.tunnel_name(domain)
        plan = grid()
        plan.add_row(
            "Tunnel",
            f"{name}, created in your Cloudflare account (Zero Trust -> Networks -> Tunnels). "
            "Cloudflare gives it a tunnel ID; the console handles that, you never type it.",
        )
        plan.add_row(
            "DNS record",
            f"in {root}: CNAME, name {label}, target <tunnel ID>.cfargotunnel.com, proxied. "
            "Added for you; any other record (www, root, mail) stays as it is.",
        )
        console.print(panel(plan, f"What the console creates for {domain}"))

    def print_add_site_guide(self, root: str, servers: list[str]) -> None:
        guide = grid()
        guide.add_row("1", "Open https://dash.cloudflare.com and sign up or log in (free).")
        guide.add_row(
            "2",
            "Left menu: Websites (on a new account the Onboard a domain button), then the "
            "Add a domain button at the top right.",
        )
        guide.add_row(
            "3",
            f"Type the root domain exactly: {root}  (not a subdomain). Keep Quick scan for DNS "
            "records selected and press Continue.",
        )
        guide.add_row("4", "Pick the Free plan at the bottom of the plan list and press Continue.")
        guide.add_row(
            "5",
            "Cloudflare lists the DNS records it found (your website, mail). Press Continue; "
            "they stay as they are.",
        )
        guide.add_row(
            "6",
            "Cloudflare now shows two nameservers, for example ada.ns.cloudflare.com and "
            "bob.ns.cloudflare.com. Copy them.",
        )
        guide.add_row(
            "7",
            f"Go to where you bought {root} (GoDaddy, Namecheap, Hostinger, Google, BigRock, "
            "GoDaddy India...): My domains -> the domain -> Nameservers (or DNS) -> Change -> "
            "Custom nameservers. Delete the old ones"
            + (f" (currently {', '.join(servers)})" if servers else "")
            + ", paste the two Cloudflare ones, save.",
        )
        guide.add_row(
            "8",
            "Back in Cloudflare press Continue / Check nameservers. It usually turns Active in a "
            "few minutes (Cloudflare also emails you), sometimes up to a few hours.",
        )
        guide.add_row(
            "Your website",
            f"keeps working: Cloudflare's scan in step 3 copies the records {root} uses today, "
            "and the brain later adds only its own subdomain record.",
        )
        guide.add_row(
            "Cannot move DNS?",
            "then keep DNS where it is and use /domain vps: an A record for the subdomain at your "
            "current DNS host, pointing at a VPS running the brain with Caddy.",
        )
        guide.add_row("Then", "choose Check again below, or run /domain again later.")
        console.print(panel(guide, f"Add {root} to Cloudflare, step by step"))

    def ask_domain_name(self, given: str = "") -> str | None:
        current = self.settings.brain_domain
        root = ".".join(current.split(".")[-2:]) if current else "yourcompany.com"
        example = f"brain.{root}"
        if not given:
            console.print(
                f"[muted]Pick a name under your domain, for example {example}: people open "
                f"https://{example}{self.settings.admin_path}, agents use https://{example}/mcp. "
                "The console adds only that one record; www, mail and the root are not touched.[/muted]"
            )
        raw = given
        for _ in range(3):
            if not raw:
                raw = prompt(f"Name for the brain (for example {example})", default=current)
            try:
                return cloudflare.valid_hostname(raw)
            except cloudflare.TunnelError:
                console.print(
                    f"[warning]{raw!r} is not a domain name. Type only the name, such as {example}: "
                    "letters, digits, dots and hyphens, no https://, no spaces.[/warning]"
                )
                raw = ""
        console.print("[muted]cancelled[/muted]")
        return None

    def verify_on_cloudflare(self, domain: str) -> str:
        shown = False
        while True:
            with console.status(f"[muted]checking the nameservers of {domain}...[/muted]"):
                root, servers = cloudflare.nameservers(domain)
            if cloudflare.on_cloudflare(servers):
                console.print(f"[success]{root} is on Cloudflare[/success]")
                return "ok"
            console.print(
                f"[warning]{root} is not on Cloudflare yet[/warning] "
                + (
                    f"[muted](its nameservers are {', '.join(servers)})[/muted]"
                    if servers
                    else "[muted](no nameservers found: the domain may not exist, may be misspelt, or DNS is still updating)[/muted]"
                )
            )
            if not shown:
                self.print_add_site_guide(root, servers)
                shown = True
            picked = choose(
                console,
                "What next?",
                [
                    Choice("Type a different name", "rename", "if this was the wrong domain"),
                    Choice("Check again", "again", "after the nameservers were changed"),
                    Choice("Open the Cloudflare dashboard", "open", "the Add a domain page"),
                    Choice("Show the steps again", "steps", "the step-by-step guide"),
                    Choice("Continue anyway", "force", "only if you are sure it is on Cloudflare"),
                ],
                0 if not servers else 1,
                ask=prompt,
                cancel="Back",
            )
            if picked is None:
                return "cancel"
            if picked.value in ("force", "rename"):
                return "ok" if picked.value == "force" else "rename"
            if picked.value == "open":
                webbrowser.open(cloudflare.DASHBOARD)
                console.print(
                    "[muted]the dashboard opened in your browser; choose Check again when done[/muted]"
                )
            elif picked.value == "steps":
                self.print_add_site_guide(root, servers)

    def print_server_steps(self, domain: str, where: str) -> None:
        update_env(
            self.env_path, BRAIN_DOMAIN=domain, PUBLIC_URL=f"https://{domain}", CLOUDFLARE_TUNNEL=""
        )
        self.named_tunnel = None
        self.reload()
        steps = grid()
        if where == "vps":
            steps.add_row(
                "1",
                f"Cloudflare DNS (or your DNS host): add an A record  {domain} -> the VPS public IP  (proxy off at first)",
            )
            steps.add_row("2", "On the VPS open ports 80 and 443 (ufw allow 80,443/tcp)")
            steps.add_row(
                "3",
                "Copy this folder to the VPS (git clone) with this same .env, then run:  docker compose up -d",
            )
            steps.add_row(
                "",
                f"Caddy obtains the HTTPS certificate; admin panel https://{domain}{self.settings.admin_path}, agents https://{domain}/mcp",
            )
            steps.add_row(
                "4",
                "Optional: turn the Cloudflare proxy on afterwards (SSL Full strict); see docs/cloudflare-domain.md",
            )
        else:
            steps.add_row(
                "1",
                "Deploy this repository's Dockerfile on the platform with a persistent volume at /data and /app/brain_files",
            )
            steps.add_row(
                "2",
                f"Set ADMIN_MASTER_KEY, PUBLIC_URL=https://{domain} and the other values from this .env in the platform",
            )
            steps.add_row(
                "3",
                f"Add {domain} as a custom domain in the platform and create the CNAME it shows in your DNS",
            )
            steps.add_row("", "docs/admin-panel-hosting.md has the per-platform details")
        steps.add_row("5", "Recommended: /protect to lock the admin panel down")
        console.print(panel(steps, f"Domain saved: {domain}", style="success"))
        if confirm(f"Check whether https://{domain} answers now?", default=False):
            self.check_domain(domain)

    def check_domain(self, domain: str) -> bool:
        with console.status(f"[muted]checking https://{domain}...[/muted]"):
            ok, detail = cloudflare.wait_healthy(f"https://{domain}", seconds=12)
        if ok:
            console.print(
                f"[success]https://{domain} answers: the brain is reachable there[/success]"
            )
        else:
            console.print(
                f"[warning]https://{domain} does not answer yet ({detail}); finish the steps above and try again later[/warning]"
            )
        return ok

    def ensure_cloudflared(self, then: str = "/domain again") -> str | None:
        if self.cloudflared.ready:
            return self.cloudflared.path
        with console.status(
            "[muted]installing cloudflared, Cloudflare's connector program...[/muted]"
        ) as status:

            def progress(done: int, total: int) -> None:
                if total:
                    status.update(
                        f"[muted]downloading cloudflared... {done * 100 // total}%[/muted]"
                    )

            try:
                binary = self.cloudflared.wait(progress)
            except cloudflare.TunnelError as error:
                binary = None
                failure = str(error)
        if binary is None:
            steps = grid()
            steps.add_row("Problem", failure)
            steps.add_row("Install by hand", cloudflare.install_instruction())
            steps.add_row("Then", then)
            console.print(panel(steps, "cloudflared could not be installed", style="warning"))
            return None
        console.print(f"[success]cloudflared installed at {binary}[/success]")
        return binary

    def connect_domain_here(self, domain: str) -> None:
        binary = self.ensure_cloudflared()
        if binary is None:
            return
        if self.tunnel is not None:
            self.tunnel.terminate()
            self.tunnel, self.tunnel_url = None, None
        others = cloudflare.running_tunnels()
        if others and confirm(
            f"{len(others)} other cloudflared process(es) are running on this computer; stop them so "
            "the new tunnel owns the domain?",
            default=True,
        ):
            cloudflare.stop_tunnels()
        root = ".".join(domain.split(".")[-2:])
        if cloudflare.cert_path().is_file():
            console.print(
                "[muted]Step 1 of 4: already signed in to Cloudflare on this computer[/muted]"
            )
        else:
            console.print(
                f"[muted]Step 1 of 4: a Cloudflare page opens in your browser. Click [/muted]"
                f"[accent]{root}[/accent][muted] in the list of your websites, then click [/muted]"
                "[accent]Authorize[/accent][muted]. Come back here afterwards; the console continues "
                "by itself. This happens once per computer.[/muted]"
            )
        if not cloudflare.login(binary):
            console.print(
                "[danger]Cloudflare login did not complete[/danger] [muted](no authorization was "
                "received; run /domain again and click Authorize in the browser)[/muted]"
            )
            return
        console.print("[success]authorized[/success]")
        name = cloudflare.tunnel_name(domain)
        try:
            with console.status(f"[muted]Step 2 of 4: creating tunnel {name}...[/muted]"):
                tunnel_id = cloudflare.ensure_tunnel(binary, name)
            with console.status(
                f"[muted]Step 3 of 4: pointing {domain} at the tunnel in Cloudflare DNS...[/muted]"
            ):
                cloudflare.route_dns(binary, name, domain)
        except cloudflare.TunnelError as error:
            console.print(f"[danger]{error}[/danger]")
            return
        credentials = cloudflare.credentials_path(tunnel_id)
        if credentials is None:
            console.print(
                f"[danger]the credentials file for tunnel {tunnel_id} is missing in {cloudflare.cloudflared_home()}; "
                "delete the tunnel in Cloudflare Zero Trust and run /domain again[/danger]"
            )
            return
        config = cloudflare.write_config(
            self.env_path.parent / "data",
            name,
            tunnel_id,
            credentials,
            domain,
            self.handle.local_url,
        )
        update_env(
            self.env_path,
            BRAIN_DOMAIN=domain,
            PUBLIC_URL=f"https://{domain}",
            CLOUDFLARE_TUNNEL=name,
        )
        self.reload()
        self.tunnel = None if self.attached else cloudflare.start(binary, config, name)
        self.named_tunnel = name
        self.tunnel_url = None
        with console.status(
            f"[muted]Step 4 of 4: waiting for https://{domain} to answer...[/muted]"
        ):
            ok, detail = cloudflare.wait_healthy(f"https://{domain}", seconds=90)
        body = grid()
        body.add_row("Admin panel", f"[accent]https://{domain}{self.settings.admin_path}[/accent]")
        body.add_row("MCP endpoint", f"[accent]https://{domain}/mcp[/accent]")
        body.add_row(
            "Tunnel",
            f"{name}  [muted]ID {tunnel_id}; DNS target {tunnel_id}.cfargotunnel.com; config {config}[/muted]",
        )
        body.add_row(
            "Runs",
            "in the background service, also after a reboot. Keys you create now use this domain."
            if self.attached
            else "while this console runs; it starts again with the next brain. Keys you create "
            "now use this domain.",
        )
        body.add_row(
            "Next",
            "/protect to lock the admin panel down; /key to create keys for agents elsewhere",
        )
        if ok:
            console.print(panel(body, f"{domain} is live", style="success"))
        elif detail == "HTTP 404":
            body.add_row(
                "Status",
                "[warning]Cloudflare reaches a tunnel for this name but it answers 404: another "
                "tunnel or an older cloudflared process is bound to it. Stop other cloudflared "
                "processes, delete the old tunnel in Cloudflare Zero Trust -> Networks -> Tunnels, "
                f"then run /domain again (log: {config.with_suffix('.log')})[/warning]",
            )
            console.print(panel(body, f"{domain} connected, but routed elsewhere", style="warning"))
        else:
            body.add_row(
                "Status",
                f"[warning]not answering yet ({detail}); DNS can take a minute, check again with "
                "/domain[/warning]",
            )
            console.print(panel(body, f"{domain} connected, waiting for DNS", style="warning"))
        if not self.attached and confirm(
            f"Keep https://{domain} up after this console closes and after a reboot? "
            "(installs the background service)",
            default=True,
        ):
            self.service_install()

    def cmd_service(self, args: list[str]) -> None:
        actions: dict[str, Callable[[], None]] = {
            "install": self.service_install,
            "start": self.service_start,
            "stop": self.service_stop,
            "restart": self.service_restart,
            "status": self.service_status,
            "logs": self.service_logs,
            "uninstall": self.service_uninstall,
        }
        action = args[0].casefold() if args else ""
        if action not in actions:
            state = background.status(self.spec())
            picked = choose(
                console,
                "Background service",
                [
                    Choice("Status", "status", state.detail),
                    Choice("Install", "install", "run in the background, start at login or boot"),
                    Choice("Logs", "logs", str(self.spec().log_path)),
                    Choice("Stop", "stop", "stop the service; this console serves instead"),
                    Choice("Start", "start", "start the installed service again"),
                    Choice("Uninstall", "uninstall", "remove the service"),
                ],
                0 if state.installed else 1,
                ask=prompt,
                cancel="Back",
            )
            if picked is None:
                return
            action = picked.value
        actions[action]()

    def service_status(self) -> None:
        state = background.status(self.spec())
        body = grid()
        style = "success" if state.installed and state.healthy else "warning"
        body.add_row("State", f"[{style}]{state.detail}[/{style}]")
        if state.drifted:
            body.add_row(
                "Definition",
                "[warning]out of date (code or Python moved); /service install refreshes it[/warning]",
            )
        for label, value in background.describe(self.spec()):
            body.add_row(label, value)
        console.print(panel(body, "Background service"))

    def service_install(self) -> None:
        spec = self.spec()
        intro = grid()
        intro.add_row(
            "What it does",
            "installs the brain as a background service: it starts when you log in (at boot on a "
            "server), restarts if it crashes, and keeps your domain's tunnel up after this console "
            "closes. This console attaches to it.",
        )
        for label, value in background.describe(spec):
            intro.add_row(label, value)
        console.print(panel(intro, "Run the brain in the background"))
        if not self.attached and not confirm("Install it now?", default=True):
            console.print("[muted]nothing changed[/muted]")
            return
        self.release_server()
        try:
            with console.status("[muted]installing the background service...[/muted]"):
                state = background.install(spec)
        except background.ServiceError as error:
            console.print(f"[danger]{error}[/danger]")
            self.take_server_back()
            return
        self.attach()
        console.print(f"[success]{state.detail}[/success]")
        self.status_block()

    def service_start(self) -> None:
        self.release_server()
        try:
            with console.status("[muted]starting the background service...[/muted]"):
                state = background.start(self.spec())
        except background.ServiceError as error:
            console.print(f"[danger]{error}[/danger]")
            self.take_server_back()
            return
        self.attach()
        console.print(f"[success]{state.detail}[/success]")

    def service_stop(self) -> None:
        with console.status("[muted]stopping the background service...[/muted]"):
            background.stop(self.spec())
        console.print("[success]background service stopped[/success]")
        self.take_server_back()
        console.print(
            "[muted]this console now serves the brain until you leave; /service start brings the "
            "service back[/muted]"
        )

    def service_restart(self) -> None:
        if not self.attached:
            console.print(
                "[warning]no background service is serving right now; /restart restarts this "
                "console's server[/warning]"
            )
            return
        self.reload()
        console.print("[success]background service restarted[/success]")

    def service_logs(self) -> None:
        text = background.logs(self.spec(), 60)
        if not text:
            console.print(f"[muted]no log yet at {self.spec().log_path}[/muted]")
            return
        console.print(panel(Text(text), str(self.spec().log_path)))

    def service_uninstall(self) -> None:
        if not confirm(
            "Remove the background service? The brain then runs only while this console is open.",
            default=False,
        ):
            console.print("[muted]nothing changed[/muted]")
            return
        with console.status("[muted]removing the background service...[/muted]"):
            background.uninstall(self.spec())
        console.print("[success]background service removed[/success]")
        self.take_server_back()

    def release_server(self) -> None:
        if self.attached:
            return
        if self.tunnel is not None:
            self.tunnel.terminate()
            self.tunnel = None
        self.handle.stop()

    def take_server_back(self) -> None:
        self.attached = False
        if background.healthy(self.handle.local_url):
            self.attached = True
            return
        with console.status("[muted]starting the server in this console...[/muted]"):
            self.handle.start()
        if self.settings.cloudflare_tunnel and self.settings.brain_domain:
            self.resume_named_tunnel()
        if self.client is not None:
            self.client.close()
        self.client = AdminClient(self.handle.local_url, self.settings.admin_master_key)

    def attach(self) -> None:
        self.attached = True
        self.tunnel = None
        if self.settings.cloudflare_tunnel and self.settings.brain_domain:
            self.named_tunnel = self.settings.cloudflare_tunnel
        if self.client is not None:
            self.client.close()
        self.client = AdminClient(self.handle.local_url, self.settings.admin_master_key)

    def resume_named_tunnel(self) -> None:
        name = self.settings.cloudflare_tunnel
        config = self.env_path.parent / "data" / f"cloudflared-{name}.yml"
        if not config.is_file():
            console.print(
                f"[warning]tunnel {name} is configured but its config file is missing; "
                "/domain repairs it[/warning]"
            )
            return
        binary = self.ensure_cloudflared()
        if binary is None:
            return
        self.tunnel = cloudflare.start(binary, config, name)
        self.named_tunnel = name

    def cmd_protect(self, args: list[str]) -> None:
        body = grid()
        body.add_row(
            "Always", "the admin panel needs the master key; without it every call answers 401"
        )
        body.add_row("Path", "serve the panel at an unguessable path instead of /admin")
        body.add_row("IPs", "allow the panel only from your office or VPN addresses (CIDR allowed)")
        body.add_row("Lockout", "lock an address out after this many wrong keys per minute")
        body.add_row(
            "Cloudflare Access",
            "[muted]free login layer in front of the path; see docs/cloudflare-domain.md[/muted]",
        )
        console.print(panel(body, "Protect the admin panel"))
        path = prompt("Admin path", default=self.settings.admin_path)
        ips = prompt(
            "Allowed IPs or CIDRs, comma separated (empty = everyone)",
            default=self.settings.admin_allowed_ips,
        )
        throttle = prompt(
            "Failed attempts per minute before lockout",
            default=str(self.settings.admin_login_failures_per_minute),
        )
        update_env(
            self.env_path,
            ADMIN_PATH="/" + path.strip("/"),
            ADMIN_ALLOWED_IPS=ips,
            ADMIN_LOGIN_FAILURES_PER_MINUTE=throttle or "10",
        )
        self.reload()
        console.print(f"[success]saved and restarted.[/success] Admin panel: {self.admin_url}")

    def cmd_security(self, args: list[str]) -> None:
        local_only = self.settings.brain_host in ("127.0.0.1", "localhost")
        exposure = (
            "this computer only"
            if local_only
            else "every network interface (reachable on your LAN, and publicly if the port is open)"
        )
        body = grid()
        body.add_row(
            "Master key",
            f"{mask(self.settings.admin_master_key, 'ADMIN_MASTER_KEY')}   [muted]/master shows it, /rotate replaces it[/muted]",
        )
        body.add_row("Admin path", f"{self.settings.admin_path}   [muted]/protect[/muted]")
        body.add_row(
            "Allowed IPs",
            self.settings.admin_allowed_ips
            or "[muted]everyone (master key still required)[/muted]",
        )
        body.add_row(
            "Lockout after",
            f"{self.settings.admin_login_failures_per_minute} failed attempts per minute",
        )
        body.add_row("Listening on", f"{self.settings.brain_host}  [muted]{exposure}[/muted]")
        body.add_row(
            "LLM key",
            "[success]set[/success]" if self.settings.llm_api_key else "[muted]none[/muted]",
        )
        console.print(panel(body, "Security and admin access"))
        if not local_only and confirm(
            "Listen on this computer only (127.0.0.1)? Remote agents then need /tunnel or /domain.",
            default=False,
        ):
            update_env(self.env_path, BRAIN_HOST="127.0.0.1")
            self.reload()
            console.print("[success]now listening on 127.0.0.1 only[/success]")

    def cmd_master(self, args: list[str]) -> None:
        body = grid()
        body.add_row("Master key", f"[accent]{self.settings.admin_master_key}[/accent]")
        body.add_row("Use it at", self.admin_url)
        body.add_row("Stored in", str(self.env_path))
        console.print(panel(body, "Master key (admin access)"))

    def cmd_rotate(self, args: list[str]) -> None:
        if confirm(
            "Generate a new master key? Anyone using the old one, including open panel sessions, must sign in again.",
            default=False,
        ):
            new_key = generate_master_key()
            try:
                self.api.reseal_keys(new_key)
            except (RuntimeError, httpx.HTTPError) as error:
                console.print(
                    f"[warning]stored agent keys could not be re-sealed ({error}); /agent offers "
                    "replacement keys instead[/warning]"
                )
            update_env(self.env_path, ADMIN_MASTER_KEY=new_key)
            self.reload()
            console.print(
                f"[success]new master key:[/success] [accent]{self.settings.admin_master_key}[/accent]"
            )

    def cmd_env(self, args: list[str]) -> None:
        values = read_env(self.env_path)
        table = listing(str(self.env_path), "Key", "Value")
        for key in EDITABLE_KEYS:
            table.add_row(
                f"[accent]{key}[/accent]", mask(values.get(key, ""), key) or "[muted]unset[/muted]"
            )
        for key in SECRET_KEYS:
            table.add_row(
                f"[accent]{key}[/accent]", mask(values.get(key, ""), key) or "[muted]unset[/muted]"
            )
        console.print(table)
        console.print(
            "[muted]/set KEY value changes one and restarts; /edit opens the file[/muted]"
        )

    def cmd_set(self, args: list[str]) -> None:
        if not args:
            console.print("[warning]usage: /set KEY value[/warning]")
            return
        key = args[0].upper()
        if key not in EDITABLE_KEYS + SECRET_KEYS:
            console.print(
                f"[warning]choose one of: {', '.join(EDITABLE_KEYS + SECRET_KEYS)}[/warning]"
            )
            return
        value = " ".join(args[1:]).strip()
        if not value and key not in SECRET_KEYS:
            value = prompt(f"New value for {key}", default=read_env(self.env_path).get(key, ""))
        update_env(self.env_path, **{key: value})
        self.reload()
        console.print(f"[success]{key} saved and the server restarted[/success]")

    def cmd_edit(self, args: list[str]) -> None:
        editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "nano")
        subprocess.call([editor, str(self.env_path)])
        if confirm("Restart the server to apply changes?", default=True):
            self.reload()
            console.print("[success]restarted[/success]")

    def cmd_restart(self, args: list[str]) -> None:
        self.reload()
        console.print("[success]restarted[/success]")

    def run_shell(self, line: str) -> None:
        tokens = tokenize(line)
        if not tokens:
            return
        name, args = tokens[0].casefold(), tokens[1:]
        builtins: dict[str, Callable[[list[str]], None]] = {
            "ls": self.sh_ls,
            "ll": self.sh_ls,
            "cat": self.sh_cat,
            "cd": self.sh_cd,
            "pwd": self.sh_pwd,
            "grep": self.sh_grep,
            "head": self.sh_head,
            "tail": self.sh_tail,
            "clear": self.cmd_clear,
            "cls": self.cmd_clear,
        }
        handler = builtins.get(name)
        if handler is not None:
            try:
                handler(args)
            except OSError as error:
                console.print(f"[danger]{name}: {error}[/danger]")
            return
        try:
            code = subprocess.call(line, shell=True, cwd=str(self.cwd))
        except OSError as error:
            console.print(f"[danger]{error}[/danger]")
            return
        if code:
            console.print(f"[muted]exit code {code}[/muted]")

    def resolve_path(self, value: str | None) -> Path:
        return (self.cwd / (value or ".")).resolve()

    def sh_ls(self, args: list[str]) -> None:
        positional = [a for a in args if not a.startswith("-")]
        show_hidden = any(a.startswith("-") and "a" in a for a in args)
        path = self.resolve_path(positional[0] if positional else None)
        if path.is_file():
            console.print(Text(path.name))
            return
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        table = Table.grid(padding=(0, 3))
        table.add_column(style="accent")
        table.add_column(justify="right", style="muted")
        for entry in entries:
            if entry.name.startswith(".") and not show_hidden:
                continue
            if entry.is_dir():
                table.add_row(Text(entry.name + "/", style="accent"), "")
            else:
                table.add_row(Text(entry.name, style="default"), human_size(entry.stat().st_size))
        console.print(table)

    def sh_cat(self, args: list[str]) -> None:
        files = [a for a in args if not a.startswith("-")]
        if not files:
            console.print("[warning]usage: cat <file>[/warning]")
            return
        for name in files:
            path = self.resolve_path(name)
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(files) > 1:
                console.print(f"[primary]==> {name} <==[/primary]")
            console.print(markdown_lines(text) if path.suffix.casefold() == ".md" else Text(text))

    def sh_cd(self, args: list[str]) -> None:
        target = self.resolve_path(args[0]) if args else Path.home()
        if not target.is_dir():
            console.print(f"[danger]cd: no such directory: {args[0] if args else target}[/danger]")
            return
        self.cwd = target
        os.chdir(target)
        console.print(f"[muted]{target}[/muted]")

    def sh_pwd(self, args: list[str]) -> None:
        console.print(Text(str(self.cwd)))

    def sh_head(self, args: list[str], from_end: bool = False) -> None:
        count = 10
        files: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            if token == "-n" and index + 1 < len(args):
                count = int(args[index + 1])
                index += 2
                continue
            if token.startswith("-") and token[1:].isdigit():
                count = int(token[1:])
            else:
                files.append(token)
            index += 1
        if not files:
            console.print("[warning]usage: head [-n N] <file>[/warning]")
            return
        for name in files:
            lines = (
                self.resolve_path(name).read_text(encoding="utf-8", errors="replace").splitlines()
            )
            chosen = lines[-count:] if from_end else lines[:count]
            if len(files) > 1:
                console.print(f"[primary]==> {name} <==[/primary]")
            console.print(Text("\n".join(chosen)))

    def sh_tail(self, args: list[str]) -> None:
        self.sh_head(args, from_end=True)

    def sh_grep(self, args: list[str]) -> None:
        flags = {a for a in args if a.startswith("-") and len(a) > 1}
        rest = [a for a in args if a not in flags]
        if not rest:
            console.print("[warning]usage: grep [-i] [-n] [-l] <pattern> [path ...][/warning]")
            return
        ignore_case = any("i" in f for f in flags)
        pattern = re.compile(
            re.escape(rest[0]) if "-F" in flags else rest[0], re.IGNORECASE if ignore_case else 0
        )
        targets = [self.resolve_path(p) for p in rest[1:]] or [self.cwd]
        names_only = any(f == "-l" for f in flags)
        matches = 0
        for target in targets:
            for path in walk_files(target):
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                label = (
                    str(path.relative_to(self.cwd)) if path.is_relative_to(self.cwd) else str(path)
                )
                for number, line in enumerate(lines, 1):
                    if pattern.search(line):
                        matches += 1
                        if names_only:
                            console.print(Text(label, style="accent"))
                            break
                        out = Text()
                        out.append(label, style="accent")
                        out.append(f":{number}:", style="muted")
                        start = 0
                        for found in pattern.finditer(line):
                            out.append(line[start : found.start()])
                            out.append(found.group(0), style="warning")
                            start = found.end()
                        out.append(line[start:])
                        console.print(out)
                        if matches >= 500:
                            console.print("[muted]... stopping after 500 matches[/muted]")
                            return
        if not matches:
            console.print("[muted]no matches[/muted]")


def walk_files(target: Path) -> Iterator[Path]:
    if target.is_file():
        yield target
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_WALK)
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.casefold() in (".png", ".jpg", ".gif", ".zip", ".db", ".pyc", ".exe"):
                continue
            yield path


def slug_hint(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return cleaned[:40] or "project"


def domain_live(domain: str) -> bool:
    try:
        response = httpx.get(f"https://{domain}/healthz", timeout=3, follow_redirects=True)
        return response.status_code == 200 and bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def code_stamp() -> float:
    try:
        return max(path.stat().st_mtime for path in Path(__file__).resolve().parent.glob("*.py"))
    except (OSError, ValueError):
        return 0.0


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def run_shell(open_browser: bool = False) -> None:
    settings = get_settings()
    Shell(settings, Path.cwd() / ".env", open_browser=open_browser).run()
