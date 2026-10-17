from typer.testing import CliRunner

from second_brain.cli import app, ensure_env, write_env


def test_write_env_and_ensure_env(tmp_path):
    key = write_env(tmp_path, "http://localhost:8000/")
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"ADMIN_MASTER_KEY={key}" in content and "PUBLIC_URL=http://localhost:8000\n" in content
    assert (tmp_path / "data").is_dir() and (tmp_path / "brain_files").is_dir()
    assert ensure_env(tmp_path, "http://localhost:8000") == key
    (tmp_path / ".env").write_text("BRAIN_PORT=8010\nADMIN_MASTER_KEY=\n", encoding="utf-8")
    fresh = ensure_env(tmp_path, "http://localhost:8010")
    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert fresh.startswith("bm_") and fresh != key
    assert "BRAIN_PORT=8010" in content and content.count("ADMIN_MASTER_KEY=") == 1


def test_init_command(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["init", str(tmp_path), "--public-url", "http://127.0.0.1:9000"])
    assert result.exit_code == 0, result.output
    assert "master key: bm_" in result.output
    again = runner.invoke(app, ["init", str(tmp_path)])
    assert again.exit_code == 1 and "already exists" in again.output
    forced = runner.invoke(app, ["init", str(tmp_path), "--force"])
    assert forced.exit_code == 0


def test_help_and_version():
    runner = CliRunner()
    assert "key" in runner.invoke(app, ["--help"]).output
    assert runner.invoke(app, ["--version"]).output.strip()


def test_setup_wizard_local_creates_env_project_and_key(tmp_path, monkeypatch):
    import second_brain.cli as cli

    monkeypatch.chdir(tmp_path)
    answers = iter(["1", "Acme Corp", "Sales Hermes on VPS 1"])
    monkeypatch.setattr(cli, "free_port", lambda preferred: preferred)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    printed: list[dict] = []
    monkeypatch.setattr(cli, "print_key", lambda result: printed.append(result))
    installed: list[int] = []
    monkeypatch.setattr(
        cli, "_install_background", lambda directory, port: installed.append(port) or True
    )
    cli.reset_settings_cache()
    try:
        assert cli.setup_wizard(tmp_path, 8000) is True
    finally:
        cli.reset_settings_cache()
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "BRAIN_PORT=8000" in env and "PUBLIC_URL=http://localhost:8000" in env
    assert printed and printed[0]["project"]["project_id"] == "acme-corp"
    assert printed[0]["role"] == "sales" and printed[0]["mcp_url"] == "http://localhost:8000/mcp"
    assert (tmp_path / "brain_files" / "acme-corp" / "AGENT_CHANGELOG.md").is_file()
    assert installed == [8000]


def test_service_commands_report_state(tmp_path, monkeypatch):
    from second_brain import background

    monkeypatch.chdir(tmp_path)
    state = background.Status(
        "systemd", False, False, False, None, False, tmp_path / "unit", "not installed"
    )
    monkeypatch.setattr(background, "status", lambda spec: state)

    def refuse(spec):
        raise background.ServiceError("no systemd here")

    monkeypatch.setattr(background, "install", refuse)
    runner = CliRunner()
    result = runner.invoke(app, ["service", "status"])
    assert result.exit_code == 0 and "not installed" in result.output
    result = runner.invoke(app, ["service", "install"])
    assert result.exit_code == 1 and "no systemd here" in result.output
    result = runner.invoke(app, ["service", "logs"])
    assert result.exit_code == 0 and "no log yet" in result.output


def test_brain_home_is_created_automatically(tmp_path, monkeypatch):
    from pathlib import Path

    from second_brain import config

    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    empty = tmp_path / "somewhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert config.brain_home() == tmp_path / "user" / ".brain"
    home = config.enter_home()
    assert home.is_dir() and Path.cwd().resolve() == home.resolve()
    monkeypatch.chdir(empty)
    (empty / ".env").write_text("ADMIN_MASTER_KEY=bm_x\n", encoding="utf-8")
    assert config.brain_home() == empty and config.enter_home() == empty
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "explicit"))
    assert config.enter_home() == tmp_path / "explicit"
    assert (tmp_path / "explicit").is_dir() and Path.cwd().resolve() == (
        tmp_path / "explicit"
    ).resolve()
    runner = CliRunner()
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "explicit" / ".env").is_file()


def test_console_start_offers_the_update(tmp_path, monkeypatch, capsys):
    import second_brain.cli as cli
    from second_brain import updates

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr(updates, "latest_version", lambda timeout=3.0: "99.0.0")
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(updates, "run_upgrade", lambda method=None: (True, "", ["x"]))
    launched: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda args: launched.append(args) or 0)
    try:
        cli.offer_update()
        raised = False
    except cli.typer.Exit:
        raised = True
    assert raised and launched and launched[0][1:3] == ["-m", "second_brain.cli"]
    assert "A new version" in capsys.readouterr().out
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: False)
    cli.offer_update()
    assert "Skipped" in capsys.readouterr().out
    monkeypatch.setattr(updates, "latest_version", lambda timeout=3.0: updates.__version__)
    (tmp_path / "data" / "update-check.json").unlink(missing_ok=True)
    cli.offer_update()
    assert capsys.readouterr().out == ""


def test_update_command_reports_newest_release(tmp_path, monkeypatch):
    from second_brain import updates

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(updates, "latest_version", lambda timeout=3.0: updates.__version__)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0 and "newest release" in result.output
    monkeypatch.setattr(updates, "latest_version", lambda timeout=3.0: "99.0.0")
    monkeypatch.setattr(updates, "run_upgrade", lambda method=None: (True, "ok", ["x"]))
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0 and "upgraded to 99.0.0" in result.output


def test_print_banner_renders(tmp_path, capsys):
    import second_brain.cli as cli
    from second_brain.config import Settings

    settings = Settings(
        _env_file=None, admin_master_key="bm_abcdefghijklmnop", brain_domain="brain.example.com"
    )
    cli.print_banner(settings, 8000, ["acme-corp"], verbose=False)
    out = capsys.readouterr().out
    assert "AI Computer Brain is running" in out and "/admin" in out and "acme-corp" in out


def test_env_read_update_and_mask(tmp_path):
    from second_brain.tui import mask, read_env, update_env

    env = tmp_path / ".env"
    env.write_text(
        "BRAIN_PORT=8000\n# comment\nPUBLIC_URL=http://localhost:8000\n", encoding="utf-8"
    )
    update_env(env, PUBLIC_URL="https://brain.example.com", BRAIN_DOMAIN="brain.example.com")
    text = env.read_text(encoding="utf-8")
    assert text.splitlines() == [
        "BRAIN_PORT=8000",
        "# comment",
        "PUBLIC_URL=https://brain.example.com",
        "BRAIN_DOMAIN=brain.example.com",
    ]
    assert read_env(env)["BRAIN_DOMAIN"] == "brain.example.com"
    assert mask("bm_abcdefghijklmnop", "ADMIN_MASTER_KEY") == "bm_...mnop"
    assert mask("postgresql://u:secret@h/db", "DATABASE_URL") == "postgresql://u:***@h/db"
    assert mask("info", "LOG_LEVEL") == "info"


def test_theme_palettes_and_picker(monkeypatch):
    import second_brain.tui as tui

    assert len(tui.PALETTES) >= 5 and len({p.key for p in tui.PALETTES}) == len(tui.PALETTES)
    assert tui.palette_for("forest").name == "Forest" and tui.palette_for("nope").key == "ocean"
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: "3")
    assert tui.choose_theme() == "forest"
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: "violet")
    assert tui.choose_theme() == "violet"
    palette = tui.apply_theme("ember")
    assert palette.key == "ember" and tui.current.key == "ember"
    tui.apply_theme("ocean")
