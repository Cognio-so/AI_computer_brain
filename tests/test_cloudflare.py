import json

import pytest

from second_brain import cloudflare


def test_hostnames_and_tunnel_names():
    assert cloudflare.valid_hostname(" https://Brain.YourCompany.com/ ") == "brain.yourcompany.com"
    assert cloudflare.valid_hostname("memory.acme.co") == "memory.acme.co"
    assert cloudflare.valid_hostname("[memory.acme.co]") == "memory.acme.co"
    for bad in ("localhost", "brain", "http://", "a b.com", ""):
        with pytest.raises(cloudflare.TunnelError):
            cloudflare.valid_hostname(bad)
    assert cloudflare.tunnel_name("brain.yourcompany.com") == "brain-yourcompany-com"
    assert cloudflare.tunnel_name("memory.acme.co") == "brain-memory-acme-co"


def test_tunnel_listing_and_config(tmp_path):
    listing = json.dumps(
        [
            {"id": "old", "name": "brain-x", "deleted_at": "2026-01-01"},
            {"id": "11111111-2222-3333-4444-555555555555", "name": "brain-x"},
        ]
    )
    assert cloudflare.find_tunnel_id(listing, "brain-x") == "11111111-2222-3333-4444-555555555555"
    assert cloudflare.find_tunnel_id(listing, "other") is None
    assert cloudflare.find_tunnel_id("not json", "x") is None
    assert cloudflare.CREATED_RE.search(
        "Created tunnel brain-x with id 11111111-2222-3333-4444-555555555555"
    )
    creds = tmp_path / "11111111.json"
    creds.write_text("{}", encoding="utf-8")
    config = cloudflare.write_config(
        tmp_path / "data", "brain-x", "1111", creds, "brain.example.com", "http://127.0.0.1:8000"
    )
    text = config.read_text(encoding="utf-8")
    assert "tunnel: 1111" in text and "hostname: brain.example.com" in text
    assert "service: http://127.0.0.1:8000" in text and "http_status:404" in text
    assert cloudflare.describe_tunnel("brain-x", "brain.example.com", None)["running"] is False
    assert cloudflare.install_instruction()


def test_ensure_tunnel_and_route_use_cloudflared(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run(binary, *args, timeout=120):
        calls.append(args)
        if args[:2] == ("tunnel", "create"):
            return 1, "failed to create tunnel: tunnel with name already exists"
        if args[:2] == ("tunnel", "list"):
            return 0, json.dumps([{"id": "abc-123", "name": args and "brain-d"}])
        if args[:3] == ("tunnel", "route", "dns") and "--overwrite-dns" in args:
            return 1, "flag provided but not defined: -overwrite-dns"
        if args[:3] == ("tunnel", "route", "dns"):
            return 0, "Added CNAME brain.example.com"
        return 1, "unexpected"

    monkeypatch.setattr(cloudflare, "run", fake_run)
    assert cloudflare.ensure_tunnel("cloudflared", "brain-d") == "abc-123"
    assert cloudflare.route_dns("cloudflared", "brain-d", "brain.example.com").startswith("Added")
    assert calls[0][:3] == ("tunnel", "create", "brain-d")
    assert ("tunnel", "route", "dns", "brain-d", "brain.example.com") in calls
    monkeypatch.setattr(cloudflare, "run", lambda *a, **k: (1, "boom"))
    with pytest.raises(cloudflare.TunnelError):
        cloudflare.ensure_tunnel("cloudflared", "brain-d")


def test_assets_binary_lookup_and_install(tmp_path, monkeypatch):
    assert cloudflare.asset_name("win32", "AMD64") == "cloudflared-windows-amd64.exe"
    assert cloudflare.asset_name("darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
    assert cloudflare.asset_name("linux", "x86_64") == "cloudflared-linux-amd64"
    assert cloudflare.asset_name("linux", "aarch64") == "cloudflared-linux-arm64"
    monkeypatch.setattr(cloudflare.shutil, "which", lambda name: None)
    monkeypatch.setattr(cloudflare, "WINDOWS_PATHS", ())
    assert cloudflare.cloudflared_binary([tmp_path]) is None
    local = tmp_path / cloudflare.binary_name()
    local.write_bytes(b"x")
    assert cloudflare.cloudflared_binary([tmp_path]) == str(local)
    downloaded: list[str] = []

    def fake_download(url, destination, progress=None):
        downloaded.append(url)
        destination.write_bytes(b"binary")
        if progress:
            progress(6, 6)

    monkeypatch.setattr(cloudflare, "_download", fake_download)
    monkeypatch.setattr(cloudflare, "run", lambda *a, **k: (0, "cloudflared version 2026.1.0"))
    monkeypatch.setattr(cloudflare.sys, "platform", "linux")
    monkeypatch.setattr(cloudflare.platform, "machine", lambda: "x86_64")
    target = cloudflare.install(tmp_path / "bin")
    assert downloaded == [cloudflare.RELEASES + "cloudflared-linux-amd64"]
    assert (
        target.endswith("cloudflared")
        and (tmp_path / "bin" / "cloudflared").read_bytes() == b"binary"
    )
    monkeypatch.setattr(cloudflare, "run", lambda *a, **k: (1, "segfault"))
    with pytest.raises(cloudflare.TunnelError):
        cloudflare.install(tmp_path / "bin2")
    message = cloudflare.classify_error(
        "failed to add route: zone not found for brain.example.com", "brain.example.com"
    )
    assert "not managed by Cloudflare DNS" in message and "example.com" in message
    assert cloudflare.classify_error("some other failure", "x.com") == "some other failure"


def test_running_tunnels_parses_process_lists(monkeypatch):
    class Completed:
        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(cloudflare.sys, "platform", "win32")
    monkeypatch.setattr(
        cloudflare.subprocess,
        "run",
        lambda *a, **k: Completed('"cloudflared.exe","4242","Console","1","80,000 K"\n'),
    )
    assert cloudflare.running_tunnels() == [4242]
    monkeypatch.setattr(cloudflare.sys, "platform", "linux")
    monkeypatch.setattr(cloudflare.subprocess, "run", lambda *a, **k: Completed("11\n12\n"))
    assert cloudflare.running_tunnels() == [11, 12]
    killed: list[int] = []
    monkeypatch.setattr(cloudflare.os, "kill", lambda pid, sig: killed.append(pid))
    assert cloudflare.stop_tunnels() == 2 and killed == [11, 12]


def test_installer_downloads_once_and_is_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudflare.shutil, "which", lambda name: None)
    monkeypatch.setattr(cloudflare, "WINDOWS_PATHS", ())
    calls: list[str] = []

    def fake_install(directory, progress=None):
        calls.append(str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / cloudflare.binary_name()
        target.write_bytes(b"x")
        return str(target)

    monkeypatch.setattr(cloudflare, "install", fake_install)
    tool = cloudflare.installer(tmp_path / "bin")
    assert tool is cloudflare.installer(tmp_path / "bin") and tool.ready is None
    assert tool.start() is True
    assert tool.wait() == str(tmp_path / "bin" / cloudflare.binary_name())
    assert tool.start() is False and tool.ready and len(calls) == 1

    def failing(directory, progress=None):
        raise cloudflare.TunnelError("offline")

    monkeypatch.setattr(cloudflare, "install", failing)
    broken = cloudflare.installer(tmp_path / "other")
    assert broken.start() is True
    with pytest.raises(cloudflare.TunnelError):
        broken.wait()
