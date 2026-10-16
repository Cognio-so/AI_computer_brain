from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from .config import CANONICAL_FILES, PERMISSIONS_FILE, Settings
from .markdown_engine import Document, parse, short_hash
from .rbac import Policy, fail_closed_policy, parse_policy

ARCHIVE_RE = re.compile(r"^logs/(?:archive|changelog)-\d{4}-\d{2}\.md$")
REPLACE_ATTEMPTS = 40
REPLACE_RETRY_SECONDS = 0.025
METADATA_DIR = ".brain"
METADATA_FILE = "project.json"
TEMPLATE_VERSION = 1


class VaultError(ValueError):
    pass


class FileNotAllowed(VaultError):
    pass


def validate_file_name(name: str, allow_archive: bool = False) -> str:
    normalized = name.strip().replace("\\", "/").lstrip("/")
    if normalized in CANONICAL_FILES:
        return normalized
    if allow_archive and ARCHIVE_RE.match(normalized):
        return normalized
    upper = normalized.upper()
    if not upper.endswith(".MD"):
        upper += ".MD"
    for canonical in CANONICAL_FILES:
        if canonical.upper() == upper:
            return canonical
    raise FileNotAllowed(f"{name!r} is not one of the canonical brain files")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as temp:
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        _replace_with_retry(temp_name, path)
    except BaseException:
        with contextlib_suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _replace_with_retry(source: str, target: Path) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


class contextlib_suppress:
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self._exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exceptions)


class Vault:
    def __init__(self, project_id: str, root: Path) -> None:
        self.project_id = project_id
        self.root = root
        self._policy_cache: tuple[str, Policy] | None = None

    def path(self, file_name: str, allow_archive: bool = False) -> Path:
        return self.root / validate_file_name(file_name, allow_archive=allow_archive)

    def exists(self) -> bool:
        return all((self.root / name).is_file() for name in CANONICAL_FILES)

    def read_text(self, file_name: str, allow_archive: bool = False) -> str:
        return self.path(file_name, allow_archive).read_text(encoding="utf-8")

    def write_text(self, file_name: str, text: str) -> None:
        _write_atomic(self.path(file_name), text)

    async def read(self, file_name: str, allow_archive: bool = False) -> str:
        return await asyncio.to_thread(self.read_text, file_name, allow_archive)

    async def write(self, file_name: str, text: str) -> None:
        await asyncio.to_thread(self.write_text, file_name, text)

    async def document(self, file_name: str) -> Document:
        return parse(await self.read(file_name))

    async def policy(self) -> Policy:
        try:
            text = await self.read(PERMISSIONS_FILE)
        except FileNotFoundError:
            return fail_closed_policy()
        digest = short_hash(text)
        if self._policy_cache and self._policy_cache[0] == digest:
            return self._policy_cache[1]
        try:
            policy = parse_policy(text)
        except Exception:
            policy = fail_closed_policy(digest)
        self._policy_cache = (digest, policy)
        return policy

    async def index(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for name in CANONICAL_FILES:
            doc = await self.document(name)
            result.append({"file": name, "etag": doc.etag, "sections": doc.index()})
        return result

    def archive_path(self, month: str, prefix: str = "archive") -> Path:
        return self.path(f"logs/{prefix}-{month}.md", allow_archive=True)

    async def append_archive(self, month: str, lines: list[str], prefix: str = "archive") -> None:
        if not lines:
            return
        path = self.archive_path(month, prefix)
        title = "Archive" if prefix == "archive" else prefix.replace("-", " ").title()

        def _append() -> None:
            existing = (
                path.read_text(encoding="utf-8") if path.exists() else f"# {title} {month}\n\n"
            )
            fresh = [line for line in lines if line.strip() and line not in existing]
            if not fresh:
                return
            _write_atomic(path, existing.rstrip("\n") + "\n" + "\n".join(fresh) + "\n")

        await asyncio.to_thread(_append)

    def metadata(self) -> dict[str, object]:
        path = self.root / METADATA_DIR / METADATA_FILE
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def list_archives(self) -> list[str]:
        logs = self.root / "logs"
        if not logs.is_dir():
            return []
        return sorted(
            f"logs/{p.name}" for p in logs.glob("*-*.md") if ARCHIVE_RE.match(f"logs/{p.name}")
        )


class VaultManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.brain_files_dir
        self.template_dir = settings.vault_template_dir
        self._vaults: dict[str, Vault] = {}

    def storage_path(self, project_id: str) -> Path:
        return self.base / project_id

    def vault_for(self, project_id: str, storage_path: str | Path | None = None) -> Vault:
        vault = self._vaults.get(project_id)
        if vault is None:
            root = Path(storage_path) if storage_path else self.storage_path(project_id)
            vault = Vault(project_id, root)
            self._vaults[project_id] = vault
        return vault

    def forget(self, project_id: str) -> None:
        self._vaults.pop(project_id, None)

    async def provision(self, project_id: str, created_at: str) -> Vault:
        root = self.storage_path(project_id)

        def _seed() -> None:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            for name in CANONICAL_FILES:
                target = root / name
                if not target.exists():
                    shutil.copyfile(self.template_dir / name, target)
            (root / "logs").mkdir(exist_ok=True)
            meta_dir = root / METADATA_DIR
            meta_dir.mkdir(exist_ok=True)
            metadata = {
                "project_id": project_id,
                "template_version": TEMPLATE_VERSION,
                "created_at": created_at,
            }
            (meta_dir / METADATA_FILE).write_text(
                json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
            )

        await asyncio.to_thread(_seed)
        self.forget(project_id)
        return self.vault_for(project_id, root)

    async def destroy(self, project_id: str) -> None:
        root = self.storage_path(project_id)
        self.forget(project_id)
        if root.exists():
            await asyncio.to_thread(shutil.rmtree, root, True)

    async def export_zip(self, project_id: str) -> bytes:
        vault = self.vault_for(project_id)

        def _pack() -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in CANONICAL_FILES:
                    archive.write(vault.path(name), name)
                for archived in vault.list_archives():
                    archive.write(vault.path(archived, allow_archive=True), archived)
                meta = vault.root / METADATA_DIR / METADATA_FILE
                if meta.exists():
                    archive.write(meta, f"{METADATA_DIR}/{METADATA_FILE}")
            return buffer.getvalue()

        return await asyncio.to_thread(_pack)

    async def import_zip(self, project_id: str, data: bytes) -> list[str]:
        vault = self.vault_for(project_id)

        def _unpack() -> list[str]:
            written: list[str] = []
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.namelist():
                    try:
                        name = validate_file_name(member, allow_archive=True)
                    except FileNotAllowed:
                        continue
                    text = archive.read(member).decode("utf-8")
                    _write_atomic(vault.path(name, allow_archive=True), text)
                    written.append(name)
            return written

        return await asyncio.to_thread(_unpack)
