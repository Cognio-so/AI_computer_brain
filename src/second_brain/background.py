from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

SERVICE_ID = "ai-computer-brain"
LAUNCHD_LABEL = "com.cogniolabs.brain"
TASK_NAME = "AI Computer Brain"
KINDS = {"win32": "windows", "darwin": "launchd"}


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Spec:
    directory: Path
    port: int
    python: str = sys.executable
    system: bool = False

    @property
    def log_path(self) -> Path:
        return self.directory / "data" / "logs" / "brain.log"

    @property
    def command(self) -> list[str]:
        return [self.python, "-m", "second_brain.cli", "serve", "--no-wizard"]

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class Status:
    kind: str
    installed: bool
    running: bool
    healthy: bool
    pid: int | None
    drifted: bool
    definition: Path
    detail: str


def kind() -> str:
    return KINDS.get(sys.platform, "systemd")


def pid_path(directory: Path) -> Path:
    return directory / "data" / "brain.pid"


def write_pid(directory: Path) -> Path:
    path = pid_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def clear_pid(directory: Path) -> None:
    path = pid_path(directory)
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def read_pid(directory: Path) -> int | None:
    try:
        value = pid_path(directory).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        code, output = run("tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH")
        return code == 0 and f'"{pid}"' in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_is_brain(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        code, output = run("tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH")
        image = output.split('","', 1)[0].strip('"').casefold() if code == 0 else ""
        return image in ("python.exe", "pythonw.exe", "brain.exe")
    if sys.platform == "darwin":
        code, output = run("ps", "-p", str(pid), "-o", "command=")
        return code == 0 and "second_brain" in output
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"second_brain" in cmdline


def healthy(url: str) -> bool:
    try:
        response = httpx.get(f"{url}/healthz", timeout=3)
        return response.status_code == 200 and bool(response.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def run(*args: str, timeout: float = 60) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        raise ServiceError(f"{args[0]} timed out") from error
    except OSError as error:
        raise ServiceError(f"could not run {args[0]}: {error}") from error
    return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()


def quoted(value: str) -> str:
    return f'"{value}"' if " " in value else value


def uid() -> int:
    getter = getattr(os, "getuid", None)
    return int(getter()) if getter else 0


def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def systemd_unit_path(spec: Spec) -> Path:
    if spec.system:
        return Path("/etc/systemd/system") / f"{SERVICE_ID}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_ID}.service"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def vbs_path(spec: Spec) -> Path:
    return spec.directory / "data" / "bin" / "brain-service.vbs"


def task_xml_path(spec: Spec) -> Path:
    return spec.directory / "data" / "bin" / "brain-service.xml"


def render_systemd(spec: Spec) -> str:
    exec_start = " ".join(quoted(part) for part in spec.command)
    user = f"User={getpass.getuser()}\n" if spec.system else ""
    target = "multi-user.target" if spec.system else "default.target"
    return (
        "[Unit]\n"
        "Description=AI Computer Brain (MCP server, admin panel, Cloudflare tunnel)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{user}"
        f"WorkingDirectory={spec.directory}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"StandardOutput=append:{spec.log_path}\n"
        f"StandardError=append:{spec.log_path}\n\n"
        "[Install]\n"
        f"WantedBy={target}\n"
    )


def render_launchd(spec: Spec) -> str:
    arguments = "".join(f"    <string>{escape(part)}</string>\n" for part in spec.command)
    path_value = escape(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{arguments}  </array>\n"
        f"  <key>WorkingDirectory</key><string>{escape(str(spec.directory))}</string>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        f"  <key>StandardOutPath</key><string>{escape(str(spec.log_path))}</string>\n"
        f"  <key>StandardErrorPath</key><string>{escape(str(spec.log_path))}</string>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n"
        f"    <key>PATH</key><string>{path_value}</string>\n"
        "    <key>PYTHONUNBUFFERED</key><string>1</string>\n"
        "  </dict>\n</dict>\n</plist>\n"
    )


def windows_command(spec: Spec) -> str:
    inner = " ".join(quoted(part) for part in spec.command)
    return f'cmd /c "{inner} >> "{spec.log_path}" 2>&1"'


def render_vbs(spec: Spec) -> str:
    literal = windows_command(spec).replace('"', '""')
    return (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.CurrentDirectory = "{spec.directory}"\n'
        f'shell.Run "{literal}", 0, False\n'
    )


def render_task_xml(spec: Spec, user: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>AI Computer Brain: MCP server, admin panel and Cloudflare tunnel</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        f"    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        f'    <Principal id="Author"><UserId>{escape(user)}</UserId>'
        "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>wscript.exe</Command>\n"
        f'      <Arguments>"{escape(str(vbs_path(spec)))}"</Arguments>\n'
        f"      <WorkingDirectory>{escape(str(spec.directory))}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def definition_path(spec: Spec) -> Path:
    current = kind()
    if current == "systemd":
        return systemd_unit_path(spec)
    if current == "launchd":
        return launchd_plist_path()
    return vbs_path(spec)


def render(spec: Spec) -> str:
    current = kind()
    if current == "systemd":
        return render_systemd(spec)
    if current == "launchd":
        return render_launchd(spec)
    return render_vbs(spec)


def systemctl(spec: Spec, *args: str) -> tuple[int, str]:
    scope = () if spec.system else ("--user",)
    return run("systemctl", *scope, *args)


def check(code: int, output: str, what: str) -> None:
    if code != 0:
        raise ServiceError(f"{what} failed: {output[-400:] or 'no output'}")


def write_definition(path: Path, text: str, encoding: str = "utf-8") -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
    except PermissionError as error:
        raise ServiceError(
            f"no permission to write {path}; run the command again with sudo"
        ) from error


def install(spec: Spec) -> Status:
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    current = kind()
    if current == "systemd":
        if shutil.which("systemctl") is None:
            raise ServiceError(
                "this system has no systemd; run brain serve under your init system or Docker"
            )
        write_definition(systemd_unit_path(spec), render_systemd(spec))
        check(*systemctl(spec, "daemon-reload"), "systemctl daemon-reload")
        check(*systemctl(spec, "enable", "--now", SERVICE_ID), "systemctl enable")
        if not spec.system:
            run("loginctl", "enable-linger", getpass.getuser())
    elif current == "launchd":
        plist = launchd_plist_path()
        run("launchctl", "bootout", f"gui/{uid()}/{LAUNCHD_LABEL}")
        write_definition(plist, render_launchd(spec))
        code, output = run("launchctl", "bootstrap", f"gui/{uid()}", str(plist))
        if code != 0:
            check(*run("launchctl", "load", "-w", str(plist)), "launchctl load")
    else:
        write_definition(vbs_path(spec), render_vbs(spec))
        write_definition(task_xml_path(spec), render_task_xml(spec, windows_user()), "utf-16")
        check(
            *run("schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", str(task_xml_path(spec))),
            "schtasks /Create",
        )
        check(*run("schtasks", "/Run", "/TN", TASK_NAME), "schtasks /Run")
    if not wait_until(lambda: healthy(spec.local_url), 60):
        raise ServiceError(
            f"the service was installed but the server did not answer on port {spec.port}; "
            f"see {spec.log_path}"
        )
    return status(spec)


def start(spec: Spec) -> Status:
    current = kind()
    if current == "systemd":
        check(*systemctl(spec, "start", SERVICE_ID), "systemctl start")
    elif current == "launchd":
        code, output = run("launchctl", "bootstrap", f"gui/{uid()}", str(launchd_plist_path()))
        if code != 0:
            run("launchctl", "kickstart", f"gui/{uid()}/{LAUNCHD_LABEL}")
    else:
        check(*run("schtasks", "/Run", "/TN", TASK_NAME), "schtasks /Run")
    if not wait_until(lambda: healthy(spec.local_url), 60):
        raise ServiceError(f"the server did not answer on port {spec.port}; see {spec.log_path}")
    return status(spec)


def kill(pid: int) -> None:
    if sys.platform == "win32":
        run("taskkill", "/PID", str(pid), "/T", "/F")
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def stop(spec: Spec) -> Status:
    current = kind()
    if current == "systemd":
        systemctl(spec, "stop", SERVICE_ID)
    elif current == "launchd":
        run("launchctl", "bootout", f"gui/{uid()}/{LAUNCHD_LABEL}")
    else:
        run("schtasks", "/End", "/TN", TASK_NAME)
    pid = read_pid(spec.directory)
    if pid and pid_alive(pid) and pid_is_brain(pid):
        kill(pid)
        wait_until(lambda: not healthy(spec.local_url) and not pid_alive(pid), 20)
    else:
        wait_until(lambda: not healthy(spec.local_url), 20)
    return status(spec)


def restart(spec: Spec) -> Status:
    stop(spec)
    return start(spec)


def uninstall(spec: Spec) -> Status:
    stop(spec)
    current = kind()
    if current == "systemd":
        systemctl(spec, "disable", SERVICE_ID)
        systemd_unit_path(spec).unlink(missing_ok=True)
        systemctl(spec, "daemon-reload")
    elif current == "launchd":
        launchd_plist_path().unlink(missing_ok=True)
    else:
        run("schtasks", "/Delete", "/F", "/TN", TASK_NAME)
        vbs_path(spec).unlink(missing_ok=True)
        task_xml_path(spec).unlink(missing_ok=True)
    return status(spec)


def registered(spec: Spec) -> bool:
    current = kind()
    if current == "windows":
        code, _ = run("schtasks", "/Query", "/TN", TASK_NAME)
        return code == 0 and vbs_path(spec).is_file()
    return definition_path(spec).is_file()


def status(spec: Spec) -> Status:
    current = kind()
    definition = definition_path(spec)
    installed = registered(spec)
    drifted = False
    if installed:
        try:
            drifted = definition.read_text(encoding="utf-8") != render(spec)
        except OSError:
            drifted = True
    pid = read_pid(spec.directory)
    running = pid_alive(pid)
    alive = healthy(spec.local_url)
    where = {
        "systemd": "systemd system service" if spec.system else "systemd user service",
        "launchd": "launchd agent",
        "windows": "Windows scheduled task at logon",
    }[current]
    if installed and alive:
        detail = f"running as a {where}" + (", definition out of date" if drifted else "")
    elif installed:
        detail = f"installed as a {where} but not answering" + (
            f" (process {pid} alive)" if running else ""
        )
    elif alive:
        detail = "a server answers on the port but no background service is installed"
    else:
        detail = "not installed"
    return Status(current, installed, running, alive, pid, drifted, definition, detail)


def logs(spec: Spec, lines: int = 50) -> str:
    try:
        text = spec.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def describe(spec: Spec) -> list[tuple[str, str]]:
    current = kind()
    if current == "systemd":
        scope = "" if spec.system else "--user "
        rows = [
            ("Runs as", "systemd system service" if spec.system else "systemd user service"),
            ("Starts", "at boot" if spec.system else "at login, and at boot once lingering is on"),
            ("Definition", str(systemd_unit_path(spec))),
            ("Check", f"systemctl {scope}status {SERVICE_ID}"),
        ]
    elif current == "launchd":
        rows = [
            ("Runs as", "launchd agent"),
            ("Starts", "at login; restarts if it crashes"),
            ("Definition", str(launchd_plist_path())),
            ("Check", f"launchctl print gui/{uid()}/{LAUNCHD_LABEL}"),
        ]
    else:
        rows = [
            ("Runs as", f"Windows scheduled task '{TASK_NAME}', hidden window"),
            ("Starts", "when you log in; restarts up to 10 times if it crashes"),
            ("Definition", str(vbs_path(spec))),
            ("Check", "Task Scheduler, or brain service status"),
        ]
    rows.append(("Logs", str(spec.log_path)))
    rows.append(("Remove", "brain service uninstall, or /service uninstall"))
    return rows
