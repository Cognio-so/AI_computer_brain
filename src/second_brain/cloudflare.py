from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
CREATED_RE = re.compile(r"id ([0-9a-f-]{36})", re.IGNORECASE)
INSTALL = {
    "win32": "winget install Cloudflare.cloudflared",
    "darwin": "brew install cloudflared",
    "linux": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
}


RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
WINDOWS_PATHS = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "cloudflared"
    / "cloudflared.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "cloudflared" / "cloudflared.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe",
)


class TunnelError(RuntimeError):
    pass


def binary_name() -> str:
    return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    system = (system or sys.platform).casefold()
    machine = (machine or platform.machine()).casefold()
    arch = (
        "arm64"
        if machine in ("arm64", "aarch64")
        else ("386" if machine in ("i386", "i686", "x86") else "amd64")
    )
    if system.startswith("win"):
        return f"cloudflared-windows-{arch}.exe"
    if system.startswith("darwin"):
        return f"cloudflared-darwin-{arch}.tgz"
    if machine.startswith("arm") and arch == "amd64":
        return "cloudflared-linux-arm"
    return f"cloudflared-linux-{arch}"


def cloudflared_binary(extra_dirs: Iterable[Path] = ()) -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for directory in extra_dirs:
        candidate = Path(directory) / binary_name()
        if candidate.is_file():
            return str(candidate)
    if sys.platform == "win32":
        for candidate in WINDOWS_PATHS:
            if candidate.is_file():
                return str(candidate)
    return None


def _download(
    url: str, destination: Path, progress: Callable[[int, int], None] | None = None
) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        if response.status_code != 200:
            raise TunnelError(f"download failed with HTTP {response.status_code}: {url}")
        total = int(response.headers.get("content-length") or 0)
        done = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 256):
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)


def install(directory: Path, progress: Callable[[int, int], None] | None = None) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    asset = asset_name()
    target = directory / binary_name()
    temp = directory / asset
    try:
        _download(RELEASES + asset, temp, progress)
    except httpx.HTTPError as error:
        raise TunnelError(f"could not download cloudflared: {error}") from error
    if asset.endswith(".tgz"):
        with tarfile.open(temp) as archive:
            member = next((m for m in archive.getmembers() if m.name.endswith("cloudflared")), None)
            if member is None:
                raise TunnelError("the cloudflared archive did not contain the binary")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise TunnelError("could not read the cloudflared binary from the archive")
            target.write_bytes(extracted.read())
        temp.unlink(missing_ok=True)
    elif temp != target:
        temp.replace(target)
    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    code, output = run(str(target), "--version", timeout=30)
    if code != 0:
        raise TunnelError(f"cloudflared was downloaded but does not run: {output[-200:]}")
    return str(target)


class Installer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.thread: threading.Thread | None = None
        self.path: str | None = cloudflared_binary([directory])

    @property
    def ready(self) -> str | None:
        if self.path is None and (self.thread is None or not self.thread.is_alive()):
            self.path = cloudflared_binary([self.directory])
        return self.path

    def start(self) -> bool:
        if self.ready or (self.thread is not None and self.thread.is_alive()):
            return False
        self.thread = threading.Thread(target=self._run, name="cloudflared-install", daemon=True)
        self.thread.start()
        return True

    def _run(self) -> None:
        try:
            self.path = install(self.directory)
        except (TunnelError, OSError, tarfile.TarError):
            self.path = None

    def wait(self, progress: Callable[[int, int], None] | None = None) -> str:
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        if self.path is None:
            self.path = install(self.directory, progress)
        return self.path


_INSTALLERS: dict[str, Installer] = {}


def installer(directory: Path) -> Installer:
    key = os.path.normcase(os.path.abspath(directory))
    found = _INSTALLERS.get(key)
    if found is None:
        found = _INSTALLERS[key] = Installer(Path(directory))
    return found


def classify_error(output: str, domain: str) -> str:
    lowered = output.casefold()
    if "zone" in lowered and (
        "not found" in lowered or "could not" in lowered or "no zone" in lowered
    ):
        root = ".".join(domain.split(".")[-2:])
        return (
            f"{root} is not managed by Cloudflare DNS. Add the site to your Cloudflare account "
            "(free plan), switch its nameservers to Cloudflare, then run /domain again."
        )
    if "cert.pem" in lowered or "login" in lowered and "run" in lowered:
        return "Cloudflare login is missing or expired; the browser sign-in will run again"
    return output[-400:]


DOH = "https://cloudflare-dns.com/dns-query"
DASHBOARD = "https://dash.cloudflare.com/?to=/:account/add-site"


def root_candidates(domain: str) -> list[str]:
    labels = domain.casefold().split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def nameservers(domain: str) -> tuple[str, list[str]]:
    for candidate in root_candidates(domain):
        try:
            response = httpx.get(
                DOH,
                params={"name": candidate, "type": "NS"},
                headers={"accept": "application/dns-json"},
                timeout=10,
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        answers = data.get("Answer") if isinstance(data, dict) else None
        if not answers:
            continue
        servers = sorted(
            str(a.get("data", "")).rstrip(".").casefold()
            for a in answers
            if isinstance(a, dict) and a.get("type") == 2
        )
        if servers:
            return candidate, servers
    return domain, []


def on_cloudflare(servers: list[str]) -> bool:
    return any(s.endswith(".ns.cloudflare.com") for s in servers)


def running_tunnels() -> list[int]:
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            pids = []
            for line in completed.stdout.splitlines():
                parts = [part.strip('"') for part in line.split('","')]
                if (
                    len(parts) > 1
                    and parts[0].casefold() == "cloudflared.exe"
                    and parts[1].isdigit()
                ):
                    pids.append(int(parts[1]))
            return pids
        completed = subprocess.run(
            ["pgrep", "-x", "cloudflared"], capture_output=True, text=True, timeout=15
        )
        return [int(p) for p in completed.stdout.split() if p.isdigit()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def stop_tunnels() -> int:
    pids = running_tunnels()
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15
                )
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return len(pids)


def install_instruction() -> str:
    return INSTALL.get(sys.platform, INSTALL["linux"])


def valid_hostname(value: str) -> str:
    cleaned = (value or "").strip().strip("[]()<>'\"`").strip().casefold()
    cleaned = cleaned.removeprefix("https://").removeprefix("http://")
    cleaned = cleaned.split("/", 1)[0]
    if not HOSTNAME_RE.match(cleaned):
        raise TunnelError(
            "enter a domain or subdomain such as brain.yourcompany.com (no scheme, no path)"
        )
    return cleaned


def tunnel_name(domain: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", domain.casefold()).strip("-")[:48]
    return slug if slug.startswith("brain-") else f"brain-{slug}"


def cloudflared_home() -> Path:
    return Path.home() / ".cloudflared"


def cert_path() -> Path:
    return cloudflared_home() / "cert.pem"


def run(binary: str, *args: str, timeout: float = 120) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        raise TunnelError(f"cloudflared {args[0]} timed out") from error
    except OSError as error:
        raise TunnelError(f"could not run cloudflared: {error}") from error
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output.strip()


def login(binary: str) -> bool:
    if cert_path().is_file():
        return True
    try:
        subprocess.call([binary, "tunnel", "login"])
    except OSError:
        return False
    return cert_path().is_file()


def find_tunnel_id(listing: str, name: str) -> str | None:
    try:
        data = json.loads(listing or "[]")
    except ValueError:
        return None
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("name") == name and not item.get("deleted_at"):
            return str(item.get("id") or "") or None
    return None


def ensure_tunnel(binary: str, name: str) -> str:
    code, output = run(binary, "tunnel", "create", name)
    if code == 0:
        match = CREATED_RE.search(output)
        if match:
            return match.group(1)
    elif "already exists" not in output:
        raise TunnelError(output[-400:] or "cloudflared tunnel create failed")
    code, listing = run(binary, "tunnel", "list", "--output", "json")
    if code != 0:
        raise TunnelError(listing[-400:] or "cloudflared tunnel list failed")
    tunnel_id = find_tunnel_id(listing, name)
    if not tunnel_id:
        raise TunnelError(f"tunnel {name} was not found in your Cloudflare account")
    return tunnel_id


def route_dns(binary: str, name: str, domain: str) -> str:
    code, output = run(binary, "tunnel", "route", "dns", "--overwrite-dns", name, domain)
    if code != 0 and "overwrite-dns" in output:
        code, output = run(binary, "tunnel", "route", "dns", name, domain)
    if code != 0 and "already exists" not in output and "already configured" not in output:
        raise TunnelError(classify_error(output, domain) or "cloudflared tunnel route dns failed")
    return output


def credentials_path(tunnel_id: str) -> Path | None:
    candidate = cloudflared_home() / f"{tunnel_id}.json"
    return candidate if candidate.is_file() else None


def render_config(tunnel_id: str, credentials: Path, domain: str, local_url: str) -> str:
    return (
        f"tunnel: {tunnel_id}\n"
        f"credentials-file: {credentials.as_posix()}\n"
        "ingress:\n"
        f"  - hostname: {domain}\n"
        f"    service: {local_url}\n"
        "  - service: http_status:404\n"
    )


def write_config(
    directory: Path, name: str, tunnel_id: str, credentials: Path, domain: str, local_url: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"cloudflared-{name}.yml"
    path.write_text(render_config(tunnel_id, credentials, domain, local_url), encoding="utf-8")
    return path


def start(binary: str, config: Path, name: str) -> subprocess.Popen[str]:
    log_file = config.with_suffix(".log").open("a", encoding="utf-8")
    return subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--config", str(config), "run", name],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_healthy(url: str, seconds: float = 90) -> tuple[bool, str]:
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/healthz", timeout=8, follow_redirects=True)
            if response.status_code == 200 and response.json().get("ok"):
                return True, "ok"
            last = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as error:
            last = error.__class__.__name__
        time.sleep(3)
    return False, last


def describe_tunnel(
    name: str, domain: str, process: subprocess.Popen[str] | None
) -> dict[str, Any]:
    return {
        "name": name,
        "domain": domain,
        "running": process is not None and process.poll() is None,
    }
