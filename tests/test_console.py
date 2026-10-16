from rich.console import Console

from second_brain import banner, tui


def test_logo_layouts_fit_the_terminal_width():
    assert banner.choose_layout(200) == (("AI COMPUTER BRAIN",), 2)
    assert banner.choose_layout(120) == (("AI COMPUTER", "BRAIN"), 2)
    assert banner.choose_layout(80)[1] == 1
    assert banner.choose_layout(40) == ((), 0)
    for width in (200, 120, 100, 80, 60):
        layout, scale = banner.choose_layout(width)
        assert banner.logo_width(width) <= width - 2
        plain = banner.render_logo("#38bdf8", "#a78bfa", width, color=False).plain
        assert max(len(line) for line in plain.splitlines()) <= width
        assert plain.count("\n") + 1 == len(layout) * (banner.ROWS + 1) - 1


def test_logo_is_ascii_and_uses_theme_colours(capsys):
    text = banner.render_logo("#fbbf24", "#f97316", 120, color=False)
    assert text.plain.isascii() and "##" in text.plain
    colors = banner.logo_colors("#fbbf24", "#f97316")
    assert colors.middle == "#fbbf24" and colors.top != colors.middle != colors.bottom
    assert banner.logo_colors("white", "bright_white").top == "white"
    console = Console(width=120, color_system=None, legacy_windows=False)
    banner.print_logo(console, "#38bdf8", "#a78bfa", "grey66", "0.1.0")
    out = capsys.readouterr().out
    assert "powered by Cognio Labs" in out and "v0.1.0" in out and out.isascii()
    narrow = banner.render_signature("#38bdf8", "grey66", "0.1.0", 60).plain
    assert narrow.count("\n") == 1 and narrow.endswith("powered by Cognio Labs")


def test_command_line_parsing_helpers():
    assert tui.tokenize('/upload "my design.md" DESIGN_SYSTEM.md -p acme') == [
        "/upload",
        "my design.md",
        "DESIGN_SYSTEM.md",
        "-p",
        "acme",
    ]
    positional, options = tui.split_options(["notes.md", "--smart", "-p", "acme", "--title=Spec"])
    assert positional == ["notes.md"]
    assert options == {"smart": "true", "project": "acme", "title": "Spec"}
    positional, options = tui.split_options(
        ["notes.md", "--title", "My Notes", "--agents", "sales-01,role:ops", "--smart", "x"]
    )
    assert positional == ["notes.md", "x"]
    assert options == {"title": "My Notes", "agents": "sales-01,role:ops", "smart": "true"}
    assert tui.guess_file("design") == "DESIGN_SYSTEM.md"
    assert tui.guess_file("Company brand guide.md") == "DESIGN_SYSTEM.md"
    assert tui.guess_file("db_schema_state.md") == "DB_SCHEMA_STATE.md"
    assert tui.guess_file("COMPANY_PULSE.md") == "COMPANY_PULSE.md"
    assert tui.guess_file("random.md") is None
    assert tui.COMMAND_INDEX["exit"].name == "quit" and tui.COMMAND_INDEX["p"].name == "projects"
    assert {c.group for c in tui.COMMANDS} == {"Brain", "Sync", "Server", "Console"}
    rendered = tui.markdown_lines("# Title\n\n- item\n```\ncode\n```\n|a|b|\n").plain
    assert rendered.startswith("# Title") and "- item" in rendered and "code" in rendered
    assert tui.human_size(512) == "512B" and tui.human_size(2048) == "2.0K"


def test_shell_builtins_and_unknown_commands(tmp_path, capsys, monkeypatch):
    from second_brain.config import Settings

    monkeypatch.chdir(tmp_path)
    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=100,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    (tmp_path / "notes.md").write_text("# Notes\n\n- price is 49\n- hello\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    settings = Settings(_env_file=None, admin_master_key="bm_test", brain_files_dir=tmp_path / "bf")
    shell = tui.Shell(settings, tmp_path / ".env")
    shell.cwd = tmp_path
    assert shell.execute("ls") is True
    assert shell.execute("cat notes.md") is True
    assert shell.execute("grep -n price notes.md") is True
    assert shell.execute("head -n 1 notes.md") is True
    assert shell.execute("cd sub") is True and shell.cwd == tmp_path / "sub"
    assert shell.execute("/typo") is True
    assert shell.execute("/help upload") is True
    assert shell.execute("quit") is False
    assert shell.execute("/exit") is False
    out = capsys.readouterr().out
    assert "notes.md" in out and "sub/" in out
    assert "price is 49" in out and "notes.md:3:" in out
    assert "unknown command /typo" in out and "/upload <path>" in out


def test_env_and_theme_helpers_still_exported():
    assert tui.read_env is not None and tui.update_env is not None
    assert tui.palette_for("gold").name == "Gold"
    assert len({p.key for p in tui.PALETTES}) == len(tui.PALETTES) >= 8


def test_ask_domain_name_explains_and_reasks(tmp_path, capsys, monkeypatch):
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=120,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    settings = Settings(_env_file=None, admin_master_key="bm_test", brain_domain="memory.acme.co")
    shell = tui.Shell(settings, tmp_path / ".env")
    answers = iter(["", "brain.acme.co"])
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: next(answers))
    assert shell.ask_domain_name("whta to do") == "brain.acme.co"
    out = capsys.readouterr().out
    assert "is not a domain name" in out
    answers = iter(["", "", ""])
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: next(answers))
    assert shell.ask_domain_name("still wrong") is None
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: "memory.acme.co")
    assert shell.ask_domain_name() == "memory.acme.co"
    out = flat(capsys.readouterr().out)
    assert "Pick a name under your domain, for example brain.acme.co" in out
    assert "www, mail and the root are not touched" in out


def flat(text: str) -> str:
    return " ".join("".join(ch if ch.isascii() else " " for ch in text).replace("|", " ").split())


def test_domain_guides_when_not_on_cloudflare(tmp_path, capsys, monkeypatch):
    from second_brain import cloudflare
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=120,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    settings = Settings(_env_file=None, admin_master_key="bm_test")
    shell = tui.Shell(settings, tmp_path / ".env")
    monkeypatch.setattr(cloudflare, "nameservers", lambda d: ("example.com", ["ns1.godaddy.com"]))
    answers = iter(["6", "1"])
    monkeypatch.setattr(tui, "prompt", lambda *a, **k: next(answers))
    assert shell.verify_on_cloudflare("brain.example.com") == "cancel"
    assert shell.verify_on_cloudflare("brain.example.com") == "rename"
    out = flat(capsys.readouterr().out)
    assert "Add example.com to Cloudflare, step by step" in out
    assert "Add a domain" in out and "Custom nameservers" in out and "ns1.godaddy.com" in out
    monkeypatch.setattr(
        cloudflare, "nameservers", lambda d: ("example.com", ["a.ns.cloudflare.com"])
    )
    assert shell.verify_on_cloudflare("brain.example.com") == "ok"
    shell.print_record_plan("brain.app.example.com")
    out = flat(capsys.readouterr().out)
    assert "example.com is on Cloudflare" in out and "name brain.app" in out and "CNAME" in out
    assert "tunnel ID" in out and "cfargotunnel.com" in out and "brain-app-example-com" in out
    assert "brain-brain" not in out


def test_console_notices_code_updates(tmp_path, capsys, monkeypatch):
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=100,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    shell = tui.Shell(Settings(_env_file=None, admin_master_key="bm_test"), tmp_path / ".env")
    assert shell.code_stamp == tui.code_stamp() > 0
    shell.code_stamp = 1.0
    monkeypatch.setattr(tui, "read_input", lambda markup: "/quit")
    assert shell.read_line() == "/quit"
    assert "updated on disk" in capsys.readouterr().out
    assert shell.read_line() == "/quit"
    assert "updated on disk" not in capsys.readouterr().out


def test_console_installs_cloudflared_automatically(tmp_path, capsys, monkeypatch):
    from second_brain import cloudflare
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=120,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    monkeypatch.setattr(cloudflare.shutil, "which", lambda name: None)
    monkeypatch.setattr(cloudflare, "WINDOWS_PATHS", ())

    def failing(directory, progress=None):
        raise cloudflare.TunnelError("offline")

    monkeypatch.setattr(cloudflare, "install", failing)
    shell = tui.Shell(Settings(_env_file=None, admin_master_key="bm_test"), tmp_path / ".env")
    assert shell.ensure_cloudflared(then="/tunnel again") is None
    out = flat(capsys.readouterr().out)
    assert "cloudflared could not be installed" in out and "/tunnel again" in out

    def fake_install(directory, progress=None):
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / cloudflare.binary_name()
        target.write_bytes(b"x")
        if progress:
            progress(1, 1)
        return str(target)

    monkeypatch.setattr(cloudflare, "install", fake_install)
    assert shell.cloudflared.start() is True
    expected = str(tmp_path / "data" / "bin" / cloudflare.binary_name())
    assert shell.ensure_cloudflared() == expected
    assert "cloudflared installed at" in capsys.readouterr().out
    assert shell.ensure_cloudflared() == expected and shell.cloudflared.start() is False


def test_service_install_attaches_console_to_the_service(tmp_path, capsys, monkeypatch):
    from second_brain import background
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=120,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    settings = Settings(_env_file=None, admin_master_key="bm_test", brain_port=8765)
    shell = tui.Shell(settings, tmp_path / ".env")
    events: list[str] = []
    monkeypatch.setattr(shell.handle, "stop", lambda: events.append("stop"))
    monkeypatch.setattr(shell.handle, "start", lambda: events.append("start"))
    monkeypatch.setattr(tui, "confirm", lambda *a, **k: True)
    state = background.Status(
        "windows", True, True, True, 4242, False, tmp_path / "x.vbs", "running as a Windows task"
    )
    monkeypatch.setattr(background, "install", lambda spec: state)
    monkeypatch.setattr(background, "status", lambda spec: state)
    monkeypatch.setattr(background, "healthy", lambda url: True)
    assert shell.execute("/service install") is True
    out = flat(capsys.readouterr().out)
    assert shell.attached is True and events == ["stop"]
    assert "Run the brain in the background" in out and "running as a Windows task" in out
    assert "starts at login or boot" in out
    monkeypatch.setattr(background, "stop", lambda spec: state)
    monkeypatch.setattr(background, "healthy", lambda url: False)
    assert shell.execute("/service stop") is True
    assert shell.attached is False and events == ["stop", "start"]
    assert "this console now serves" in flat(capsys.readouterr().out)


def test_saved_domain_is_shown_as_pending_and_can_be_forgotten(tmp_path, capsys, monkeypatch):
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=140,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )
    env = tmp_path / ".env"
    lines = [
        "ADMIN_MASTER_KEY=bm_test",
        "PUBLIC_URL=https://memory.example.com",
        "BRAIN_DOMAIN=memory.example.com",
    ]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings = Settings(
        _env_file=None, admin_master_key="bm_test", brain_domain="memory.example.com"
    )
    shell = tui.Shell(settings, env)
    shell.client = tui.AdminClient(shell.handle.local_url, "bm_test")
    monkeypatch.setattr(tui, "domain_live", lambda domain: False)
    shell.status_block()
    out = flat(capsys.readouterr().out)
    assert "saved but not connected" in out and "/domain forget" in out
    assert "http://127.0.0.1:8000/mcp" in out and "keys embed https://memory.example.com" in out
    monkeypatch.setattr(tui, "domain_live", lambda domain: True)
    shell.status_block()
    assert "live served by the server its DNS points at" in flat(capsys.readouterr().out)
    reloaded: list[str] = []
    monkeypatch.setattr(shell, "reload", lambda: reloaded.append("reload"))
    monkeypatch.setattr(tui, "confirm", lambda *a, **k: True)
    assert shell.execute("/domain forget") is True
    saved = dict(
        line.split("=", 1) for line in env.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert saved["BRAIN_DOMAIN"] == "" and saved["PUBLIC_URL"] == "http://localhost:8000"
    assert saved["CLOUDFLARE_TUNNEL"] == "" and reloaded == ["reload"]
    assert "forgot memory.example.com" in flat(capsys.readouterr().out)


def test_agent_command_shows_connection_commands_again(tmp_path, capsys, monkeypatch):
    from second_brain.config import Settings

    tui.apply_theme("mono")
    monkeypatch.setattr(
        tui,
        "console",
        Console(
            width=140,
            color_system=None,
            legacy_windows=False,
            theme=tui.rich_theme(tui.palette_for("mono")),
        ),
    )

    class FakeApi:
        def projects(self):
            return [{"project_id": "acme", "project_name": "Acme"}]

        def keys(self, pid):
            return [
                {
                    "key_id": "k1",
                    "agent_id": "sales-hermes",
                    "department_role": "sales",
                    "key_prefix": "sb_acme_sales_sales-hermes_ab12",
                    "active": True,
                    "stale": False,
                }
            ]

        def connect_info(self, pid, key_id):
            assert (pid, key_id) == ("acme", "k1")
            return {
                "agent_id": "sales-hermes",
                "role": "sales",
                "mcp_url": "https://brain.example.com/mcp",
                "placeholder": "<paste the agent key>",
                "snippets": {
                    "claude_code": "claude mcp add company-brain https://brain.example.com/mcp "
                    '--header "Authorization: Bearer <paste the agent key>"',
                    "hermes": "mcp_servers:\n  company-brain:\n    url: https://brain.example.com/mcp",
                    "openclaw": "mcporter config add company-brain",
                },
            }

    shell = tui.Shell(Settings(_env_file=None, admin_master_key="bm_test"), tmp_path / ".env")
    shell.client = FakeApi()
    shell.project_id = "acme"
    monkeypatch.setattr(tui, "confirm", lambda *a, **k: False)
    assert shell.execute("/agent sales-hermes") is True
    out = flat(capsys.readouterr().out)
    assert "Connect sales-hermes to the brain" in out and "https://brain.example.com/mcp" in out
    assert "<paste the agent key>" in out and "created before keys were kept" in out
