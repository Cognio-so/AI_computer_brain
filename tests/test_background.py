import os
import xml.etree.ElementTree as ET
from pathlib import Path

from second_brain import background


def spec_for(tmp_path, **overrides):
    python = str(tmp_path / "py thon" / "python.exe")
    return background.Spec(tmp_path / "brain", 8123, python=python, **overrides)


def test_templates_mention_the_right_things(tmp_path):
    spec = spec_for(tmp_path)
    unit = background.render_systemd(spec)
    assert "WorkingDirectory=" in unit and 'ExecStart="' in unit and "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit and "User=" not in unit
    system = background.render_systemd(spec_for(tmp_path, system=True))
    assert "WantedBy=multi-user.target" in system and "User=" in system
    plist = background.render_launchd(spec)
    assert background.LAUNCHD_LABEL in plist and "<key>KeepAlive</key><true/>" in plist
    assert "second_brain.cli" in plist and str(spec.log_path) in plist
    vbs = background.render_vbs(spec)
    assert vbs.endswith(", 0, False\n") and "second_brain.cli serve --no-wizard" in vbs
    assert '""' in vbs
    xml = background.render_task_xml(spec, "PC\\me")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml and "LogonTrigger" in xml
    assert "brain-service.vbs" in xml and "PC\\me" in xml
    assert ET.fromstring(xml.encode("utf-16")).tag.endswith("Task")
    assert background.describe(spec)[-1][0] == "Remove"


def test_pid_file_roundtrip(tmp_path):
    assert background.read_pid(tmp_path) is None
    background.write_pid(tmp_path)
    assert background.read_pid(tmp_path) == os.getpid() and background.pid_alive(os.getpid())
    background.clear_pid(tmp_path)
    assert background.read_pid(tmp_path) is None
    assert background.pid_alive(None) is False


def test_install_status_and_uninstall_for_each_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(background, "healthy", lambda url: True)
    monkeypatch.setattr(background, "pid_alive", lambda pid: False)
    monkeypatch.setattr(background, "wait_until", lambda predicate, seconds: predicate())
    monkeypatch.setattr(background.shutil, "which", lambda name: "/bin/systemctl")
    spec = spec_for(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(*args, timeout=60):
        calls.append(args)
        if args[:2] == ("schtasks", "/Query"):
            return (0 if background.vbs_path(spec).is_file() else 1), ""
        return 0, ""

    monkeypatch.setattr(background, "run", fake_run)
    cases = (
        ("systemd", background.systemd_unit_path(spec)),
        ("launchd", background.launchd_plist_path()),
        ("windows", background.vbs_path(spec)),
    )
    for current, definition in cases:
        monkeypatch.setattr(background, "kind", lambda current=current: current)
        calls.clear()
        state = background.install(spec)
        assert state.installed and state.healthy and not state.drifted and definition.is_file()
        assert state.kind == current and "running as a" in state.detail
        definition.write_text(
            definition.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8"
        )
        assert background.status(spec).drifted
        state = background.uninstall(spec)
        assert not state.installed and not definition.is_file()
    assert "<Task" in background.render_task_xml(spec, "me")
    assert ("schtasks", "/Delete", "/F", "/TN", background.TASK_NAME) in calls
    assert ("schtasks", "/Run", "/TN", background.TASK_NAME) in calls


def test_stop_kills_only_brain_processes(tmp_path, monkeypatch):
    spec = spec_for(tmp_path)
    monkeypatch.setattr(background, "kind", lambda: "windows")
    monkeypatch.setattr(background, "healthy", lambda url: False)
    monkeypatch.setattr(background, "pid_alive", lambda pid: pid in (4242, 4243))
    monkeypatch.setattr(background, "pid_is_brain", lambda pid: pid == 4242)
    monkeypatch.setattr(background, "wait_until", lambda predicate, seconds: predicate())
    monkeypatch.setattr(background, "run", lambda *a, timeout=60: (1, ""))
    killed: list[int] = []
    monkeypatch.setattr(background, "kill", lambda pid: killed.append(pid))
    pid_file = background.pid_path(spec.directory)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("4242", encoding="utf-8")
    state = background.stop(spec)
    assert killed == [4242] and not state.installed
    pid_file.write_text("4243", encoding="utf-8")
    background.stop(spec)
    assert killed == [4242]
