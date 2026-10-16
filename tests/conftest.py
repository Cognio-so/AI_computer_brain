import os

import pytest

from second_brain.auth import Authenticator, Principal
from second_brain.config import Settings
from second_brain.database import Database
from second_brain.hub import LocalHub
from second_brain.locks import LocalLockRegistry
from second_brain.service import BrainService
from second_brain.vault import VaultManager

MASTER_KEY = "bm_test-master-key-0123456789abcdef"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "brain_files_dir": tmp_path / "brain_files",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'meta.db').as_posix()}",
        "admin_master_key": MASTER_KEY,
        "git_commit_debounce_seconds": 0,
        "public_url": "http://testserver",
        "cors_origins": "",
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    settings.ensure_directories()
    return settings


async def make_service(settings: Settings) -> BrainService:
    db = Database(settings.database_url, settings.sql_dir)
    await db.init()
    service = BrainService(
        settings,
        db,
        LocalHub(),
        LocalLockRegistry(),
        VaultManager(settings),
        Authenticator(db, settings.admin_master_key),
    )
    await service.start()
    return service


@pytest.fixture(autouse=True)
def _brain_home(tmp_path, monkeypatch):
    monkeypatch.chdir(os.getcwd())
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path / "home"))


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def service(settings):
    svc = await make_service(settings)
    try:
        yield svc
    finally:
        await svc.stop()
        await svc.db.close()


@pytest.fixture
async def project(service):
    return await service.create_project("acme-corp", "Acme Corp")


@pytest.fixture
def master() -> Principal:
    return Principal("*", "admin", "admin", None, is_master=True)


async def issue(service, project, role, agent):
    record, plaintext = await service.issue_key(project, role, agent, None)
    principal = await service.auth.authenticate(plaintext)
    return principal, plaintext, record
