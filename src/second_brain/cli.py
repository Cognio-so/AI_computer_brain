from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import typer

from . import __version__, background
from .config import enter_home, generate_master_key, get_settings, reset_settings_cache

app = typer.Typer(
    help="AI Computer Brain: the shared memory that keeps every agent in sync. Run it with no arguments to set up and start the server.",
)
project_app = typer.Typer(help="Manage project workspaces.", no_args_is_help=True)
key_app = typer.Typer(help="Issue and revoke compound API keys.", no_args_is_help=True)
vault_app = typer.Typer(help="Export or import a project vault.", no_args_is_help=True)
log_app = typer.Typer(help="Inspect the activity stream.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(key_app, name="key")
app.add_typer(vault_app, name="vault")
app.add_typer(log_app, name="log")
service_app = typer.Typer(
    help="Run the brain in the background, starting at login or boot.", no_args_is_help=True
)
app.add_typer(service_app, name="service")

T = TypeVar("T")
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _run(coro_factory: Callable[[Any], Awaitable[T]]) -> T:
    from .server import build_service

    async def runner() -> T:
        service = await build_service(get_settings())
        try:
            return await coro_factory(service)
        finally:
            await service.db.close()

    return asyncio.run(runner())


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _fail(error: Exception) -> None:
    detail = getattr(error, "detail", None) or str(error)
    typer.secho(f"error: {detail}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Show the version and exit.", callback=_version, is_eager=True
    ),
) -> None:
    enter_home()
    if ctx.invoked_subcommand is None:
        if interactive():
            shell(False)
        else:
            serve(None, None, 1, False, False, False, False, False)


@app.command()
def shell(
    open_browser: bool = typer.Option(
        False, "--open", help="Also open the admin panel in your browser, signed in."
    ),
) -> None:
    from .tui import run_shell

    settings = get_settings()
    if not settings.admin_master_key:
        if interactive():
            setup_wizard(Path.cwd(), settings.brain_port, ask_browser=False)
        else:
            ensure_env(Path.cwd(), f"http://localhost:{settings.brain_port}")
        reset_settings_cache()
    run_shell(open_browser=open_browser)


@app.command()
def init(
    directory: Path = typer.Argument(
        Path("."), help="Directory that will hold .env, data/ and brain_files/."
    ),
    public_url: str = typer.Option(
        "http://localhost:8000", help="URL agents will use to reach this server."
    ),
    force: bool = typer.Option(False, help="Overwrite an existing .env."),
) -> None:
    directory = directory.resolve()
    env_path = directory / ".env"
    if env_path.exists() and not force:
        typer.echo(f".env already exists at {env_path}; use --force to regenerate")
        raise typer.Exit(code=1)
    master_key = write_env(directory, public_url)
    typer.secho("initialised AI Computer Brain", fg=typer.colors.GREEN)
    typer.echo(f"  config:     {env_path}")
    typer.echo(f"  master key: {master_key}")
    typer.echo("  next:       brain serve --open   (opens the admin panel signed in)")


def write_env(
    directory: Path, public_url: str, port: int = 8000, domain: str = "", theme: str = ""
) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    master_key = generate_master_key()
    lines = [
        "BRAIN_HOST=0.0.0.0",
        f"BRAIN_PORT={port}",
        f"PUBLIC_URL={public_url.rstrip('/')}",
        f"BRAIN_DOMAIN={domain}",
        f"BRAIN_THEME={theme}",
        f"ADMIN_MASTER_KEY={master_key}",
        "DATABASE_URL=sqlite+aiosqlite:///./data/brain_metadata.db",
        "REDIS_URL=",
        "BRAIN_FILES_DIR=./brain_files",
        "GIT_AUTHOR_NAME=AI Computer Brain",
        "GIT_AUTHOR_EMAIL=brain@localhost",
        "GIT_PUSH_ENABLED=true",
        "LOG_LEVEL=info",
    ]
    (directory / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / "data").mkdir(exist_ok=True)
    (directory / "brain_files").mkdir(exist_ok=True)
    return master_key


def ensure_env(directory: Path, public_url: str) -> str:
    env_path = directory / ".env"
    if not env_path.exists():
        return write_env(directory, public_url)
    content = env_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("ADMIN_MASTER_KEY=") and line.split("=", 1)[1].strip():
            return line.split("=", 1)[1].strip()
    master_key = generate_master_key()
    kept = [line for line in content.splitlines() if not line.startswith("ADMIN_MASTER_KEY=")]
    env_path.write_text(
        "\n".join(kept + [f"ADMIN_MASTER_KEY={master_key}"]) + "\n", encoding="utf-8"
    )
    (directory / "data").mkdir(exist_ok=True)
    (directory / "brain_files").mkdir(exist_ok=True)
    return master_key


def open_when_ready(port: int, master_key: str, admin_path: str) -> None:
    url = f"http://127.0.0.1:{port}{admin_path}?key={master_key}"

    def wait() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                    webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)

    threading.Thread(target=wait, daemon=True).start()


def interactive() -> bool:
    if os.environ.get("BRAIN_FORCE_INTERACTIVE"):
        return True
    if os.environ.get("BRAIN_NONINTERACTIVE"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def mask_key(key: str) -> str:
    return f"{key[:3]}...{key[-4:]}" if len(key) > 8 else "set"


def print_key(result: dict[str, Any]) -> None:
    typer.secho(f"\nproject:  {result['project']['project_id']}", fg=typer.colors.GREEN)
    typer.echo(f"agent:    {result['agent_id']} (role {result['role']})")
    typer.echo(f"key:      {result['plaintext']}")
    typer.echo(f"endpoint: {result['mcp_url']}")
    typer.echo("\nClaude Code:\n  " + result["snippets"]["claude_code"].strip())
    hermes = "\n".join("  " + line for line in result["snippets"]["hermes"].rstrip().splitlines())
    typer.echo("\nHermes (~/.hermes/config.yaml):\n" + hermes)
    typer.echo("\nThe key is shown once here and again in the admin panel when you create more.")


def setup_wizard(directory: Path, default_port: int, ask_browser: bool = True) -> bool:
    from . import tui

    tui.safe_stdio()
    typer.secho("\nAI Computer Brain: first-time setup", fg=typer.colors.CYAN, bold=True)
    typer.echo("A few questions. Everything can be changed later in .env or the admin panel.")
    theme = choose_theme_and_show()
    typer.echo("")
    port = typer.prompt("Port for the brain on this computer", default=default_port, type=int)
    master_key = write_env(directory, f"http://localhost:{port}", port=port, theme=theme)
    reset_settings_cache()
    typer.secho(f"\nSaved .env with a new master key: {master_key}", fg=typer.colors.GREEN)
    typer.echo(f"Folder: {directory}  (.env, data and brain_files live here)")
    typer.echo("It unlocks the admin panel and is stored in .env.\n")
    if typer.confirm("Create your first project and API key now?", default=True):
        company = typer.prompt("Company or project name")
        purpose = typer.prompt("What is the first key for", default="Claude Code laptop")
        try:
            print_key(_run(lambda service: service.quick_key(purpose, None, company, None)))
        except Exception as error:
            typer.secho(f"could not create the key: {error}", fg=typer.colors.RED, err=True)
    typer.echo(
        "\nAgents on this computer can connect right away. For agents elsewhere, type /domain in "
        "the console: it puts the brain on your own domain, from a laptop or a VPS alike, and "
        "/service keeps it running."
    )
    if typer.confirm(
        "\nRun the brain in the background, starting when you log in? "
        "(agents reach it even when the console is closed)",
        default=True,
    ):
        _install_background(directory, port)
    if not ask_browser:
        return False
    return typer.confirm(
        "Open the admin panel in your browser when the server starts?", default=True
    )


def _install_background(directory: Path, port: int) -> bool:
    spec = background.Spec(directory, port)
    try:
        state = background.install(spec)
    except background.ServiceError as error:
        typer.secho(f"  could not install the background service: {error}", fg=typer.colors.YELLOW)
        typer.echo("  The console still works; /service tries again later.")
        return False
    typer.secho(f"  {state.detail}", fg=typer.colors.GREEN)
    for label, value in background.describe(spec):
        typer.echo(f"  {label}: {value}")
    return True


def choose_theme_and_show() -> str:
    from . import tui
    from .banner import print_logo

    theme = tui.choose_theme()
    palette = tui.apply_theme(theme)
    print_logo(tui.console, palette.primary, palette.accent, palette.muted, __version__)
    return theme


def _project_names() -> list[str]:
    try:
        return _run(lambda service: _names(service))
    except Exception:
        return []


async def _names(service: Any) -> list[str]:
    return [p.project_id for p in await service.list_projects()]


def print_banner(settings: Any, port: int, projects: list[str], verbose: bool) -> None:
    from . import tui
    from .banner import print_logo

    tui.safe_stdio()
    palette = tui.apply_theme(settings.brain_theme)
    print_logo(tui.console, palette.primary, palette.accent, palette.muted, __version__)
    local = f"http://127.0.0.1:{port}"
    table = tui.grid()
    table.add_row(
        "Admin panel", f"[link={local}{settings.admin_path}]{local}{settings.admin_path}[/link]"
    )
    table.add_row("MCP endpoint", f"{local}/mcp")
    if settings.brain_domain:
        table.add_row(
            "Public URL",
            f"https://{settings.brain_domain} [muted](through Caddy, see docker compose)[/muted]",
        )
    elif not settings.public_url.startswith(("http://localhost", "http://127.0.0.1")):
        table.add_row("Public URL", settings.public_url)
    table.add_row("Master key", f"in .env [muted]({mask_key(settings.admin_master_key)})[/muted]")
    table.add_row(
        "Projects",
        ", ".join(f"[accent]{p}[/accent]" for p in projects)
        if projects
        else "[muted]none yet[/muted]",
    )
    table.add_row(
        "Next",
        "open the admin panel and click Create API key"
        if not projects
        else 'add agents from the API keys tab, or run: brain key quick "<name>"',
    )
    footer = "Press Ctrl+C to stop." + ("" if verbose else " Add --verbose to see request logs.")
    tui.console.print(
        tui.panel(table, "AI Computer Brain is running", f"{footer} | theme {palette.name}")
    )


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind address (default from BRAIN_HOST)."),
    port: int | None = typer.Option(None, help="Port (default from BRAIN_PORT)."),
    workers: int = typer.Option(
        1, min=1, help="Uvicorn workers. More than one requires REDIS_URL."
    ),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development only)."),
    tunnel: bool = typer.Option(False, help="Expose the server through a Cloudflare quick tunnel."),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the admin panel in your browser, already signed in."
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Show request logs."),
    no_wizard: bool = typer.Option(
        False, "--no-wizard", help="Skip the first-run questions and use defaults."
    ),
) -> None:
    import uvicorn

    settings = get_settings()
    if not settings.admin_master_key:
        if interactive() and not no_wizard:
            open_browser = (
                setup_wizard(Path.cwd(), port or settings.brain_port, ask_browser=not open_browser)
                or open_browser
            )
        else:
            master_key = ensure_env(Path.cwd(), f"http://localhost:{port or settings.brain_port}")
            typer.secho(f"created .env with a new master key: {master_key}", fg=typer.colors.GREEN)
        reset_settings_cache()
        settings = get_settings()
    bind_host = host or settings.brain_host
    bind_port = port or settings.brain_port
    if background.healthy(f"http://127.0.0.1:{bind_port}"):
        typer.secho(
            f"a brain server is already running on port {bind_port} (the background service); "
            "run brain for the console, or brain service stop first",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    if workers > 1 and not settings.redis_enabled:
        _fail(RuntimeError("REDIS_URL must be set to run more than one worker"))
    logging.basicConfig(
        level=settings.log_level.upper() if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print_banner(settings, bind_port, _project_names(), verbose)
    background.write_pid(Path.cwd())
    if open_browser:
        open_when_ready(bind_port, settings.admin_master_key, settings.admin_path)
    tunnel_process = (
        _start_tunnel(bind_port, settings.cloudflare_tunnel, settings.brain_domain)
        if tunnel or settings.cloudflare_tunnel
        else None
    )
    try:
        uvicorn.run(
            "second_brain.server:create_app",
            factory=True,
            host=bind_host,
            port=bind_port,
            workers=workers if not reload else 1,
            reload=reload,
            log_level=settings.log_level if verbose else "warning",
            access_log=verbose,
            proxy_headers=True,
            forwarded_allow_ips="*",
            timeout_keep_alive=75,
        )
    finally:
        background.clear_pid(Path.cwd())
        if tunnel_process is not None:
            tunnel_process.terminate()


def _start_tunnel(port: int, named: str = "", domain: str = "") -> subprocess.Popen[str] | None:
    from .cloudflare import TunnelError, installer

    tool = installer(Path.cwd() / "data" / "bin")
    if not tool.ready:
        typer.secho(
            "cloudflared is not installed; downloading it from Cloudflare's releases...",
            fg=typer.colors.YELLOW,
        )
    try:
        binary = tool.wait()
    except TunnelError as error:
        typer.secho(f"could not install cloudflared: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    if named:
        config = Path.cwd() / "data" / f"cloudflared-{named}.yml"
        if not config.is_file():
            typer.secho(
                f"tunnel config {config} is missing; run brain and /domain",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho(f"named tunnel {named}: https://{domain}", fg=typer.colors.GREEN)
        return subprocess.Popen(
            [binary, "tunnel", "--no-autoupdate", "--config", str(config), "run", named],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    process = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            match = TUNNEL_URL_RE.search(line)
            if match:
                typer.secho(
                    f"tunnel ready: {match.group(0)}  (MCP endpoint {match.group(0)}/mcp)",
                    fg=typer.colors.GREEN,
                )

    threading.Thread(target=pump, daemon=True).start()
    return process


@project_app.command("create")
def project_create(
    project_id: str = typer.Argument(..., help="Lowercase identifier such as acme-corp."),
    name: str = typer.Option("", "--name", help="Display name."),
    git_remote: str | None = typer.Option(
        None, "--git-remote", help="Optional remote to push the vault to."
    ),
) -> None:
    async def run(service: Any) -> dict[str, Any]:
        project = await service.create_project(
            project_id.casefold(), name or project_id, git_remote
        )
        return service.project_view(project)

    try:
        _echo_json(_run(run))
    except Exception as error:
        _fail(error)


@project_app.command("list")
def project_list() -> None:
    async def run(service: Any) -> list[dict[str, Any]]:
        return [await service.project_overview(p) for p in await service.list_projects()]

    _echo_json(_run(run))


@project_app.command("delete")
def project_delete(
    project_id: str, yes: bool = typer.Option(False, "--yes", help="Skip confirmation.")
) -> None:
    if not yes and not typer.confirm(f"Delete project {project_id} and its vault permanently?"):
        raise typer.Exit()

    async def run(service: Any) -> None:
        await service.delete_project(project_id)

    try:
        _run(run)
        typer.echo(f"deleted {project_id}")
    except Exception as error:
        _fail(error)


@key_app.command("create")
def key_create(
    project: str = typer.Option(..., "--project", help="Project ID."),
    role: str = typer.Option(
        ..., "--role", help="Department role defined in ACCESS_PERMISSIONS.md."
    ),
    agent: str = typer.Option(..., "--agent", help="Agent ID such as hermes-sales-01."),
    label: str | None = typer.Option(None, "--label", help="Free-text label."),
    snippets: bool = typer.Option(False, "--snippets", help="Print connection snippets."),
) -> None:
    async def run(service: Any) -> dict[str, Any]:
        record, plaintext = await service.issue_key(
            await service.get_project(project), role.casefold(), agent.casefold(), label
        )
        result = {
            "key": record.to_dict(),
            "plaintext": plaintext,
            "mcp_url": f"{service.settings.public_url}/mcp",
        }
        if snippets:
            result["snippets"] = service.connection_snippets(plaintext)
        return result

    try:
        _echo_json(_run(run))
    except Exception as error:
        _fail(error)


@key_app.command("quick")
def key_quick(
    name: str = typer.Argument(..., help="What the key is for, e.g. 'Sales Hermes on VPS 1'."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project ID. Defaults to the only project, or one created from the name.",
    ),
    project_name: str | None = typer.Option(
        None, "--project-name", help="Create or reuse a project with this display name."
    ),
    role: str | None = typer.Option(
        None, "--role", help="Override the role derived from the name."
    ),
) -> None:
    async def run(service: Any) -> dict[str, Any]:
        return await service.quick_key(name, project, project_name, role)

    try:
        result = _run(run)
    except Exception as error:
        _fail(error)
        return
    print_key(result)


@key_app.command("list")
def key_list(project: str = typer.Option(..., "--project")) -> None:
    async def run(service: Any) -> list[dict[str, Any]]:
        return [
            k.to_dict(service.settings.stale_after_minutes)
            for k in await service.list_keys(await service.get_project(project))
        ]

    try:
        _echo_json(_run(run))
    except Exception as error:
        _fail(error)


@key_app.command("revoke")
def key_revoke(key_id: str, project: str = typer.Option(..., "--project")) -> None:
    async def run(service: Any) -> dict[str, Any]:
        return (await service.revoke_key(await service.get_project(project), key_id)).to_dict()

    try:
        _echo_json(_run(run))
    except Exception as error:
        _fail(error)


@vault_app.command("export")
def vault_export(
    project_id: str, out: Path = typer.Option(..., "--out", help="Zip file to write.")
) -> None:
    async def run(service: Any) -> bytes:
        await service.get_project(project_id)
        return await service.vaults.export_zip(project_id)

    try:
        out.write_bytes(_run(run))
        typer.echo(f"wrote {out}")
    except Exception as error:
        _fail(error)


@vault_app.command("import")
def vault_import(
    project_id: str, archive: Path = typer.Argument(..., exists=True, readable=True)
) -> None:
    data = archive.read_bytes()

    async def run(service: Any) -> list[str]:
        await service.get_project(project_id)
        written = await service.vaults.import_zip(project_id, data)
        await service.system_event(
            project_id, "vault_import", f"imported {len(written)} files from {archive.name}"
        )
        return written

    try:
        _echo_json(_run(run))
    except Exception as error:
        _fail(error)


@log_app.command("tail")
def log_tail(
    project: str = typer.Option(..., "--project"),
    url: str | None = typer.Option(None, "--url", help="Server URL (default PUBLIC_URL)."),
    key: str | None = typer.Option(None, "--key", help="Master key (default ADMIN_MASTER_KEY)."),
) -> None:
    settings = get_settings()
    base = (url or settings.public_url).rstrip("/")
    token = key or settings.admin_master_key
    if not token:
        _fail(RuntimeError("a master key is required"))
    typer.echo(f"following {base}/api/admin/projects/{project}/events  (Ctrl+C to stop)")
    try:
        with httpx.stream(
            "GET",
            f"{base}/api/admin/projects/{project}/events",
            headers={"Authorization": f"Bearer {token}"},
            timeout=None,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    typer.echo(line[6:])
    except KeyboardInterrupt:
        raise typer.Exit() from None
    except httpx.HTTPError as error:
        _fail(error)


@app.command("db-watch")
def db_watch(
    dsn: str = typer.Option(
        ..., "--dsn", help="PostgreSQL connection string, read-only user recommended."
    ),
    key: str = typer.Option(..., "--key", help="An engineering compound API key."),
    url: str | None = typer.Option(
        None, "--url", help="Brain MCP endpoint (default PUBLIC_URL/mcp)."
    ),
    interval: int = typer.Option(60, min=5, help="Seconds between schema checks."),
    schema: str = typer.Option("public", help="Database schema to introspect."),
    once: bool = typer.Option(False, help="Run a single check and exit."),
) -> None:
    from .db_watch import watch

    endpoint = url or f"{get_settings().public_url.rstrip('/')}/mcp"
    try:
        asyncio.run(watch(dsn, endpoint, key, interval, schema, once))
    except KeyboardInterrupt:
        raise typer.Exit() from None
    except Exception as error:
        _fail(error)


def _spec(system: bool = False) -> background.Spec:
    return background.Spec(Path.cwd(), get_settings().brain_port, system=system)


def _print_state(state: background.Status) -> None:
    color = typer.colors.GREEN if state.installed and state.healthy else typer.colors.YELLOW
    typer.secho(state.detail, fg=color)
    if state.drifted:
        typer.echo(
            "the saved definition differs from the current code or Python path; "
            "brain service install refreshes it"
        )


def _service_call(action: Callable[[background.Spec], background.Status], system: bool) -> None:
    try:
        state = action(_spec(system))
    except background.ServiceError as error:
        _fail(error)
        return
    _print_state(state)


@service_app.command("install")
def service_install(
    system: bool = typer.Option(
        False, "--system", help="Linux: boot-time system service (run with sudo)."
    ),
) -> None:
    _service_call(background.install, system)
    for label, value in background.describe(_spec(system)):
        typer.echo(f"{label}: {value}")


@service_app.command("start")
def service_start(system: bool = typer.Option(False, "--system")) -> None:
    _service_call(background.start, system)


@service_app.command("stop")
def service_stop(system: bool = typer.Option(False, "--system")) -> None:
    _service_call(background.stop, system)


@service_app.command("restart")
def service_restart(system: bool = typer.Option(False, "--system")) -> None:
    _service_call(background.restart, system)


@service_app.command("status")
def service_status(system: bool = typer.Option(False, "--system")) -> None:
    _service_call(background.status, system)


@service_app.command("uninstall")
def service_uninstall(system: bool = typer.Option(False, "--system")) -> None:
    _service_call(background.uninstall, system)


@service_app.command("logs")
def service_logs(lines: int = typer.Option(50, help="Number of lines to show.")) -> None:
    text = background.logs(_spec(), lines)
    typer.echo(text or f"no log yet at {_spec().log_path}")


@app.command()
def doctor() -> None:
    reset_settings_cache()
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("git", shutil.which("git") is not None, shutil.which("git") or "not found"))
    from .cloudflare import cloudflared_binary

    cloudflared_path = cloudflared_binary([Path.cwd() / "data" / "bin"])
    checks.append(
        (
            "cloudflared",
            cloudflared_path is not None,
            cloudflared_path or "optional; /domain and /tunnel install it automatically",
        )
    )
    state = background.status(background.Spec(Path.cwd(), settings.brain_port))
    checks.append(("background service", state.installed and state.healthy, state.detail))
    checks.append(
        ("templates", settings.vault_template_dir.is_dir(), str(settings.vault_template_dir))
    )
    checks.append(
        ("dashboard", settings.admin_dashboard_path.is_file(), str(settings.admin_dashboard_path))
    )
    checks.append(
        (
            "master key",
            settings.admin_enabled,
            "set" if settings.admin_enabled else "empty: run brain init",
        )
    )
    try:
        settings.ensure_directories()
        probe = settings.brain_files_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("brain_files writable", True, str(settings.brain_files_dir)))
    except OSError as error:
        checks.append(("brain_files writable", False, str(error)))

    async def probe_services() -> None:
        from .database import Database

        db = Database(settings.database_url, settings.sql_dir)
        try:
            await db.init()
            checks.append(("database", await db.ping(), db.url.split("@")[-1]))
        except Exception as error:
            checks.append(("database", False, str(error)))
        finally:
            await db.close()
        if settings.redis_enabled:
            from redis.asyncio import Redis

            client = Redis.from_url(settings.redis_url)
            try:
                checks.append(("redis", bool(await client.ping()), settings.redis_url))
            except Exception as error:
                checks.append(("redis", False, str(error)))
            finally:
                await client.aclose()
        else:
            checks.append(("redis", True, "not configured, in-process hub"))
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{settings.public_url}/healthz")
                checks.append(
                    (
                        "public url",
                        response.status_code == 200,
                        f"{settings.public_url}/healthz -> {response.status_code}",
                    )
                )
        except Exception as error:
            checks.append(
                ("public url", False, f"{settings.public_url}: {error.__class__.__name__}")
            )

    asyncio.run(probe_services())
    failures = 0
    for name, ok, detail in checks:
        colour = typer.colors.GREEN if ok else typer.colors.RED
        typer.secho(f"{'ok  ' if ok else 'FAIL'} {name:<22} {detail}", fg=colour)
        failures += 0 if ok else 1
    raise typer.Exit(code=1 if failures else 0)


def entrypoint() -> None:
    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app()


if __name__ == "__main__":
    entrypoint()
