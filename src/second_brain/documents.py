from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import re
import secrets
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .auth import Principal
from .hub import Event
from .llm import LLMClient, LLMConfig, LLMError, extract_json
from .markdown_engine import slugify
from .models import Document, Project, utcnow

if TYPE_CHECKING:
    from .service import BrainService

log = logging.getLogger("brain.documents")
DOCS_DIR = "docs"
EXTRACTED_DIR = ".extracted"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
DEFAULT_READ_CHARS = 6000
MAX_READ_CHARS = 20000
SNIPPET_RADIUS = 160
TEXT_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "application/xml",
    ".rst": "text/x-rst",
    ".log": "text/plain",
}
RICH_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
}
DESCRIBE_PROMPT = """You index a document that a company admin uploaded for its AI agents. Return ONE JSON object and nothing else: {"summary": "two or three sentences saying what the document is, what it covers and when an agent should read it", "topics": ["five to ten short keywords or phrases an agent would search for"], "departments": ["which of engineering, sales, marketing, ops, design the document matters to; use all when it concerns everyone"]}. Plain ASCII, no code fences."""
MAX_DESCRIBE_CHARS = 12000
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
WORD_PARAGRAPH_RE = re.compile(r"</w:p>|<w:br/>|<w:tab/>", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"[ \t]+\n")
BLANKS_RE = re.compile(r"\n{3,}")


class DocumentError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def supported_suffixes() -> list[str]:
    return sorted({*TEXT_TYPES, *RICH_TYPES})


def normalize_access(values: Any) -> list[str]:
    if values is None:
        return ["*"]
    if isinstance(values, str):
        raw = values.strip()
        if raw.startswith("["):
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                values = [raw]
        else:
            values = raw.split(",")
    cleaned: list[str] = []
    for value in values:
        token = str(value).strip().casefold()
        if not token:
            continue
        if token in ("*", "all", "everyone"):
            return ["*"]
        if token not in cleaned:
            cleaned.append(token)
    return cleaned or ["*"]


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = WHITESPACE_RE.sub("\n", text)
    text = BLANKS_RE.sub("\n\n", text)
    return text.strip() + "\n"


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise DocumentError(
            500, "PDF support needs the pypdf package: pip install pypdf"
        ) from error
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as error:
        raise DocumentError(400, f"could not read the PDF: {error}") from error
    text = "\n\n".join(f"[page {n}]\n{p.strip()}" for n, p in enumerate(pages, 1) if p.strip())
    if not text.strip():
        raise DocumentError(
            422, "the PDF has no extractable text (scanned image?); upload a text version"
        )
    return text


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as error:
        raise DocumentError(400, "could not read the .docx file") from error
    text = WORD_PARAGRAPH_RE.sub("\n", xml)
    return html.unescape(TAG_RE.sub("", text))


def _html_text(data: bytes) -> str:
    raw = data.decode("utf-8", "replace")
    raw = SCRIPT_RE.sub("", raw)
    raw = re.sub(r"</(p|div|h[1-6]|li|tr|br|section|article)>", "\n", raw, flags=re.IGNORECASE)
    return html.unescape(TAG_RE.sub("", raw))


def extract_text(original_name: str, data: bytes) -> tuple[str, str]:
    suffix = Path(original_name).suffix.casefold()
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError(413, "the document is larger than 20 MB")
    if suffix in TEXT_TYPES:
        text = data.decode("utf-8-sig", "replace")
        media = TEXT_TYPES[suffix]
    elif suffix == ".pdf":
        text, media = _pdf_text(data), RICH_TYPES[suffix]
    elif suffix == ".docx":
        text, media = _docx_text(data), RICH_TYPES[suffix]
    elif suffix in (".html", ".htm"):
        text, media = _html_text(data), RICH_TYPES[suffix]
    else:
        raise DocumentError(
            415,
            f"unsupported file type {suffix or '(none)'}; use {', '.join(supported_suffixes())}",
        )
    text = _clean_text(text)
    if not text.strip():
        raise DocumentError(400, "the document is empty")
    return text[:MAX_TEXT_CHARS], media


class DocumentStore:
    def __init__(self, service: BrainService) -> None:
        self.service = service

    def folder(self, project: Project) -> Path:
        return self.service.vault(project).root / DOCS_DIR

    def _paths(self, project: Project, document: Document) -> tuple[Path, Path]:
        folder = self.folder(project)
        return folder / document.file_name, folder / EXTRACTED_DIR / f"{document.doc_id}.txt"

    async def list(self, project: Project) -> list[Document]:
        async with self.service.db.session() as session:
            rows = await session.scalars(
                select(Document)
                .where(Document.project_id == project.project_id)
                .order_by(Document.created_at.desc(), Document.doc_id)
            )
            return list(rows)

    async def visible(self, project: Project, principal: Principal) -> list[Document]:
        documents = await self.list(project)
        if principal.is_master:
            return documents
        role = self.service.role_of(principal)
        return [d for d in documents if d.allows(principal.agent_id, role)]

    async def get(self, project: Project, doc_id: str) -> Document:
        async with self.service.db.session() as session:
            document = await session.get(Document, doc_id)
        if document is None or document.project_id != project.project_id:
            raise DocumentError(404, f"document {doc_id!r} not found")
        return document

    async def add(
        self,
        project: Project,
        data: bytes,
        original_name: str,
        title: str | None = None,
        access: Any = None,
        uploaded_by: str = "admin",
    ) -> Document:
        text, media = await asyncio.to_thread(extract_text, original_name, data)
        doc_id = secrets.token_hex(6)
        stem = Path(original_name).stem
        suffix = Path(original_name).suffix.casefold()
        file_name = f"{slugify(stem)[:48] or 'document'}-{doc_id[:6]}{suffix}"
        clean_title = " ".join((title or stem).split())[:160] or file_name
        allowed = normalize_access(access)
        folder = self.folder(project)
        original_path = folder / file_name
        extracted_path = folder / EXTRACTED_DIR / f"{doc_id}.txt"

        def _write() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            original_path.write_bytes(data)
            if media not in TEXT_TYPES.values():
                extracted_path.parent.mkdir(parents=True, exist_ok=True)
                extracted_path.write_text(text, encoding="utf-8")

        await asyncio.to_thread(_write)
        document = Document(
            doc_id=doc_id,
            project_id=project.project_id,
            title=clean_title,
            file_name=file_name,
            original_name=original_name[:200],
            media_type=media,
            size_bytes=len(data),
            text_chars=len(text),
            content_hash=hashlib.sha256(data).hexdigest(),
            access_json=json.dumps(allowed),
            uploaded_by=uploaded_by,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        async with self.service.db.transaction() as session:
            session.add(document)
        await self._announce(
            project,
            "document_added",
            document,
            f"New document '{clean_title}' ({self._audience(allowed)}): read it with read_document",
        )
        if LLMConfig.from_settings(self.service.settings).enabled:
            self.service.spawn(self.describe_quietly(project, document.doc_id, text))
        return document

    async def describe_quietly(self, project: Project, doc_id: str, text: str) -> None:
        try:
            await self.describe(project, doc_id, text)
        except DocumentError as error:
            log.warning("document %s not indexed: %s", doc_id, error.detail)
        except Exception:
            log.exception("document %s not indexed", doc_id)

    async def describe(
        self, project: Project, doc_id: str, text: str | None = None
    ) -> dict[str, Any]:
        config = LLMConfig.from_settings(self.service.settings)
        if not config.enabled:
            raise DocumentError(409, "connect an LLM first")
        document = await self.get(project, doc_id)
        body = text if text is not None else await self.text(project, document)
        prompt = (
            f"Title: {document.title}\nFile: {document.original_name}\n\n"
            f"{body[:MAX_DESCRIBE_CHARS]}"
        )
        try:
            raw = await LLMClient(config).complete(DESCRIBE_PROMPT, prompt, 1200)
            data = extract_json(raw)
        except LLMError as error:
            log.warning("could not describe document %s: %s", doc_id, error.detail)
            raise DocumentError(error.status, error.detail) from error
        meta = {
            "summary": " ".join(str(data.get("summary") or "").split())[:600],
            "topics": [str(t).strip()[:40] for t in (data.get("topics") or []) if str(t).strip()][
                :12
            ],
            "departments": [
                str(d).strip().casefold() for d in (data.get("departments") or []) if str(d).strip()
            ][:6],
            "model": config.resolved_model,
            "at": utcnow().isoformat() + "Z",
        }
        async with self.service.db.transaction() as session:
            row = await session.get(Document, doc_id)
            if row is not None:
                row.summary = json.dumps(meta)
                row.updated_at = utcnow()
                session.add(row)
        await self.service.hub.publish(
            Event(
                "document_described",
                project.project_id,
                file=f"{DOCS_DIR}/{document.file_name}",
                agent_id="assistant",
                department="admin",
                summary=f"Document '{document.title}' indexed: {meta['summary'][:100]}",
                importance="low",
            )
        )
        return meta

    async def update(
        self, project: Project, doc_id: str, title: str | None = None, access: Any = None
    ) -> Document:
        async with self.service.db.transaction() as session:
            document = await session.get(Document, doc_id)
            if document is None or document.project_id != project.project_id:
                raise DocumentError(404, f"document {doc_id!r} not found")
            if title is not None and title.strip():
                document.title = " ".join(title.split())[:160]
            if access is not None:
                document.access_json = json.dumps(normalize_access(access))
            document.updated_at = utcnow()
            session.add(document)
        await self._announce(
            project,
            "document_updated",
            document,
            f"Document '{document.title}' is now available to {self._audience(document.access)}",
        )
        return document

    async def delete(self, project: Project, doc_id: str) -> Document:
        document = await self.get(project, doc_id)
        original_path, extracted_path = self._paths(project, document)

        def _remove() -> None:
            for path in (original_path, extracted_path):
                if path.exists():
                    path.unlink()

        await asyncio.to_thread(_remove)
        async with self.service.db.transaction() as session:
            row = await session.get(Document, doc_id)
            if row is not None:
                await session.delete(row)
        await self._announce(
            project, "document_removed", document, f"Document '{document.title}' was removed"
        )
        return document

    async def text(self, project: Project, document: Document) -> str:
        original_path, extracted_path = self._paths(project, document)
        path = extracted_path if extracted_path.exists() else original_path

        def _read() -> str:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return ""

        return await asyncio.to_thread(_read)

    async def read(
        self, project: Project, principal: Principal, doc_id: str, offset: int, max_chars: int
    ) -> dict[str, Any]:
        document = await self._authorized(project, principal, doc_id)
        text = await self.text(project, document)
        start = max(0, offset)
        size = max(200, min(max_chars or DEFAULT_READ_CHARS, MAX_READ_CHARS))
        chunk = text[start : start + size]
        next_offset = start + len(chunk)
        return {
            "status": "ok",
            "doc_id": document.doc_id,
            "title": document.title,
            "file": f"{DOCS_DIR}/{document.file_name}",
            "offset": start,
            "next_offset": next_offset if next_offset < len(text) else None,
            "total_chars": len(text),
            "content": chunk,
        }

    async def search(
        self, project: Project, principal: Principal, query: str, limit: int
    ) -> dict[str, Any]:
        terms = [t for t in re.split(r"\s+", (query or "").strip().casefold()) if t]
        if not terms:
            raise DocumentError(400, "query is required")
        hits: list[dict[str, Any]] = []
        for document in await self.visible(project, principal):
            text = await self.text(project, document)
            lowered = text.casefold()
            labels = " ".join([document.title, document.summary_text, *document.topics]).casefold()
            if all(term in labels for term in terms) and not all(term in lowered for term in terms):
                hits.append(
                    {
                        "doc_id": document.doc_id,
                        "title": document.title,
                        "offset": 0,
                        "snippet": (document.summary_text or document.title)[:320],
                    }
                )
                continue
            if not all(term in lowered for term in terms):
                continue
            for match in re.finditer(re.escape(terms[0]), lowered):
                start = max(0, match.start() - SNIPPET_RADIUS)
                end = min(len(text), match.end() + SNIPPET_RADIUS)
                hits.append(
                    {
                        "doc_id": document.doc_id,
                        "title": document.title,
                        "offset": start,
                        "snippet": " ".join(text[start:end].split()),
                    }
                )
                if len([h for h in hits if h["doc_id"] == document.doc_id]) >= 3:
                    break
            if len(hits) >= max(1, min(limit, 30)):
                break
        return {"status": "ok", "query": query, "hits": hits[: max(1, min(limit, 30))]}

    async def _authorized(self, project: Project, principal: Principal, doc_id: str) -> Document:
        document = await self.get(project, doc_id)
        if principal.is_master:
            return document
        if not document.allows(principal.agent_id, self.service.role_of(principal)):
            raise DocumentError(403, "this document is not shared with your agent")
        return document

    def _audience(self, access: list[str]) -> str:
        if "*" in access:
            return "all agents"
        return ", ".join(access)

    async def _roles_for(self, project: Project, access: list[str]) -> list[str]:
        if "*" in access:
            return ["all"]
        roles: list[str] = []
        keys = await self.service.list_keys(project)
        for entry in access:
            if entry.startswith("role:"):
                role = entry[5:]
            else:
                role = next((k.department_role for k in keys if k.agent_id == entry), "")
            if role and role not in roles:
                roles.append(role)
        return roles or ["admin"]

    async def _announce(
        self, project: Project, event_type: str, document: Document, summary: str
    ) -> None:
        affects = await self._roles_for(project, document.access)
        target = f"docs://{project.project_id}/{document.file_name}"
        await self.service._commit_mutation(
            project,
            event_type=event_type,
            agent_id="admin",
            department="admin",
            subagent_role=None,
            target_uri=target,
            summary=summary[:160],
            importance="normal",
            previous_etag=None,
            new_etag=document.content_hash[:8],
            events=[
                Event(
                    event_type,
                    project.project_id,
                    file=f"{DOCS_DIR}/{document.file_name}",
                    etag=document.content_hash[:8],
                    agent_id="admin",
                    department="admin",
                    summary=summary[:160],
                    affects=affects,
                )
            ],
            affects=affects,
        )
