from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .config import REPO_ROOT

PACKAGE = "ai-computer-brain"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CACHE_SECONDS = 6 * 60 * 60


def parse(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.strip().split("."):
        leading = ""
        for ch in piece:
            if not ch.isdigit():
                break
            leading += ch
        if not leading:
            break
        parts.append(int(leading))
        if leading != piece:
            break
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse(candidate) > parse(current)


def latest_version(timeout: float = 3.0) -> str | None:
    try:
        response = httpx.get(PYPI_URL, timeout=timeout, follow_redirects=True)
        if response.status_code != 200:
            return None
        return str(response.json()["info"]["version"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def cache_path(data_dir: Path) -> Path:
    return data_dir / "update-check.json"


def check(data_dir: Path, now: float | None = None, force: bool = False) -> dict[str, object]:
    moment = now if now is not None else time.time()
    path = cache_path(data_dir)
    if not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            fresh = moment - float(cached.get("checked_at", 0)) < CACHE_SECONDS
            if fresh and cached.get("latest"):
                latest = str(cached["latest"])
                return {"current": __version__, "latest": latest, "newer": is_newer(latest)}
        except (OSError, ValueError, TypeError):
            pass
    latest = latest_version()
    if latest is None:
        return {"current": __version__, "latest": None, "newer": False}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": moment, "latest": latest}), encoding="utf-8")
    except OSError:
        pass
    return {"current": __version__, "latest": latest, "newer": is_newer(latest)}


def install_method(executable: str | None = None) -> str:
    if (REPO_ROOT / ".git").is_dir() and (REPO_ROOT / "pyproject.toml").is_file():
        return "source"
    parts = [part.casefold() for part in Path(executable or sys.executable).resolve().parts]
    if "uv" in parts and "tools" in parts:
        return "uv"
    if "pipx" in parts:
        return "pipx"
    return "pip"


def upgrade_command(method: str | None = None) -> list[str]:
    chosen = method or install_method()
    if chosen == "uv":
        return ["uv", "tool", "upgrade", PACKAGE]
    if chosen == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    if chosen == "source":
        return ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def run_upgrade(method: str | None = None) -> tuple[bool, str, list[str]]:
    command = upgrade_command(method)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace"
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error), command
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return completed.returncode == 0, output[-1200:], command
