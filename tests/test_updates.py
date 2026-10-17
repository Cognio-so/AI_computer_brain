from second_brain import updates


def test_version_compare():
    assert updates.is_newer("0.1.2", "0.1.1") and not updates.is_newer("0.1.1", "0.1.1")
    assert updates.is_newer("1.0", "0.9.9") and not updates.is_newer("0.1.0rc1", "0.1.0")
    assert updates.parse("0.1.1") == (0, 1, 1) and updates.parse("weird") == ()


def test_check_uses_a_cache_until_it_expires(tmp_path, monkeypatch):
    calls: list[int] = []

    def fake_latest(timeout=3.0):
        calls.append(1)
        return "99.9.9"

    monkeypatch.setattr(updates, "latest_version", fake_latest)
    first = updates.check(tmp_path, now=1000.0)
    assert first["newer"] is True and first["latest"] == "99.9.9" and len(calls) == 1
    second = updates.check(tmp_path, now=2000.0)
    assert second["latest"] == "99.9.9" and len(calls) == 1
    updates.check(tmp_path, now=1000.0 + updates.CACHE_SECONDS + 1)
    assert len(calls) == 2
    updates.check(tmp_path, now=1000.0, force=True)
    assert len(calls) == 3
    monkeypatch.setattr(updates, "latest_version", lambda timeout=3.0: None)
    offline = updates.check(tmp_path / "other", now=1.0)
    assert offline["latest"] is None and offline["newer"] is False


def test_install_method_and_upgrade_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "REPO_ROOT", tmp_path / "nowhere")
    uv_python = tmp_path / "share" / "uv" / "tools" / "ai-computer-brain" / "bin" / "python"
    assert updates.install_method(str(uv_python)) == "uv"
    assert (
        updates.install_method(str(tmp_path / "pipx" / "venvs" / "x" / "bin" / "python")) == "pipx"
    )
    assert updates.install_method(str(tmp_path / "venv" / "bin" / "python")) == "pip"
    assert updates.upgrade_command("uv") == ["uv", "tool", "upgrade", "ai-computer-brain"]
    assert updates.upgrade_command("pipx")[:2] == ["pipx", "upgrade"]
    assert updates.upgrade_command("pip")[-3:] == ["install", "--upgrade", "ai-computer-brain"]
    assert updates.upgrade_command("source")[:2] == ["git", "-C"]
    source = tmp_path / "repo"
    (source / ".git").mkdir(parents=True)
    (source / "pyproject.toml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(updates, "REPO_ROOT", source)
    assert updates.install_method() == "source"
