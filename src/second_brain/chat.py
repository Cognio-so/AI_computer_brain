from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .auth import Principal
from .config import ADMIN_ROLE, CANONICAL_FILES, CHANGELOG_FILE, LOG_FILE
from .llm import LLMClient, LLMConfig, LLMError, extract_json
from .markdown_engine import slugify

if TYPE_CHECKING:
    from .models import Project
    from .service import BrainService

ASSISTANT = Principal("*", ADMIN_ROLE, "assistant", None, is_master=True)
MAX_HISTORY = 16
MAX_MESSAGE_CHARS = 8000
MAX_FILE_CHARS = 14000
MAX_CONTEXT_CHARS = 110_000
MAX_DOC_HITS = 8
HIT_RADIUS = 350
RECENT_ENTRIES = 25
WRITABLE = tuple(f for f in CANONICAL_FILES if f not in (LOG_FILE, CHANGELOG_FILE))
STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "is",
    "are",
    "what",
    "how",
    "do",
    "does",
    "with",
    "about",
    "please",
    "can",
    "you",
    "we",
    "our",
    "this",
    "that",
    "it",
    "me",
    "my",
    "us",
    "be",
    "from",
    "by",
    "as",
    "at",
    "which",
    "who",
    "when",
    "where",
    "why",
    "update",
    "change",
    "add",
    "set",
    "tell",
    "show",
    "give",
    "want",
    "need",
    "should",
}

SYSTEM_PROMPT = """You are the assistant of the AI Computer Brain for one company project. The brain is a set of Markdown files that every AI agent of the company shares: the design system, tech and infrastructure, database schema, company pulse (positioning, pricing, priorities, decisions), access permissions and rules, the company log of recent changes and findings, and the agent changelog. The admin also uploaded documents; relevant passages are included when they match the question.

Answer the admin from this material. Be specific, cite the file and heading you relied on, say plainly when the brain does not contain the answer, and never invent facts, prices, colours or endpoints.

When the admin asks you to change, add, fix or remove something in the brain, propose the exact edits instead of only describing them. The admin reviews and applies them.

Return ONE JSON object and nothing else:
{
  "reply": "your answer in Markdown",
  "changes": [
    {"file": "COMPANY_PULSE.md", "heading": "Pricing Tiers", "mode": "replace", "content": "the complete new body of that section in Markdown, without the heading line", "summary": "one line (max 160 chars) for the agents explaining what changed"}
  ]
}

Rules for changes:
- Only these files may be edited: DESIGN_SYSTEM.md, TECH_AND_INFRA.md, DB_SCHEMA_STATE.md, ACCESS_PERMISSIONS.md, COMPANY_PULSE.md. COMPANY_LOG.md and AGENT_CHANGELOG.md are written by the server.
- Use an existing ## or ### heading exactly as it appears; mode "replace" rewrites its whole body, "append" adds lines at its end, "create" adds a new section with that heading at the end of the file. Never include the heading line itself in content.
- Keep everything that is still true in a replaced section; change only what the admin asked for.
- Never put secrets (keys, tokens, passwords) into the brain.
- "changes" is an empty list when the admin only asked a question. Plain ASCII punctuation, no code fences around the JSON."""


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]{2,}", text.casefold())
    seen: list[str] = []
    for word in words:
        if word not in STOPWORDS and word not in seen:
            seen.append(word)
    return seen[:8]


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters)"


def clean_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for message in messages[-MAX_HISTORY:]:
        role = str(message.get("role") or "").strip().casefold()
        content = str(message.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    if not cleaned or cleaned[-1]["role"] != "user":
        raise LLMError("the last message must come from the admin", 400)
    return cleaned


def parse_changes(raw: Any) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return changes
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file") or "").strip()
        heading = str(item.get("heading") or "").strip()
        content = str(item.get("content") or "").strip()
        mode = str(item.get("mode") or "replace").strip().casefold()
        if file_name not in WRITABLE or not heading or not content:
            continue
        if mode not in ("replace", "append", "create"):
            mode = "replace"
        summary = " ".join(str(item.get("summary") or "").split())[:160]
        changes.append(
            {
                "file": file_name,
                "heading": heading,
                "slug": slugify(heading),
                "mode": mode,
                "content": content,
                "summary": summary or f"{heading} updated by the admin through the assistant",
            }
        )
    return changes[:12]


class ChatAssistant:
    def __init__(self, service: BrainService) -> None:
        self.service = service

    async def respond(self, project: Project, messages: list[dict[str, Any]]) -> dict[str, Any]:
        history = clean_history(messages)
        config = LLMConfig.from_settings(self.service.settings)
        client = LLMClient(config)
        context, sources = await self._context(project, history[-1]["content"])
        transcript = "\n\n".join(
            f"{'Admin' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in history[:-1]
        )
        user = (
            f"Project: {project.project_name} ({project.project_id})\n\n"
            f"## Brain content\n{context}\n\n"
            + (f"## Conversation so far\n{transcript}\n\n" if transcript else "")
            + f"## Admin asks\n{history[-1]['content']}"
        )
        raw = await client.complete(SYSTEM_PROMPT, user, 16000)
        try:
            data = extract_json(raw)
            reply = str(data.get("reply") or "").strip()
            changes = parse_changes(data.get("changes"))
        except LLMError:
            reply, changes = raw.strip(), []
        if not reply:
            reply = "I could not produce an answer from the brain for that."
        return {
            "reply": reply,
            "changes": changes,
            "sources": sources,
            "model": config.resolved_model,
        }

    async def apply(self, project: Project, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .service import BrainError

        results: list[dict[str, Any]] = []
        for change in parse_changes(changes):
            try:
                try:
                    result = await self.service.update_section(
                        ASSISTANT,
                        project,
                        change["file"],
                        change["heading"],
                        change["content"],
                        change["summary"],
                        change["mode"],
                    )
                except BrainError as error:
                    if error.code != "NOT_FOUND" or change["mode"] == "create":
                        raise
                    result = await self.service.update_section(
                        ASSISTANT,
                        project,
                        change["file"],
                        change["heading"],
                        change["content"],
                        change["summary"],
                        "create",
                    )
                payload = result.to_dict()
                results.append(
                    {
                        "file": change["file"],
                        "heading": change["heading"],
                        "ok": True,
                        "etag": payload.get("new_etag"),
                        "log_id": payload.get("log_id"),
                    }
                )
            except BrainError as error:
                results.append(
                    {
                        "file": change["file"],
                        "heading": change["heading"],
                        "ok": False,
                        "error": error.detail,
                    }
                )
        return results

    async def _context(self, project: Project, question: str) -> tuple[str, list[dict[str, Any]]]:
        vault = self.service.vault(project)
        parts: list[str] = []
        sources: list[dict[str, Any]] = []
        budget = MAX_CONTEXT_CHARS
        for name in CANONICAL_FILES:
            try:
                text = await vault.read(name)
            except OSError:
                continue
            chunk = _trim(text.strip(), MAX_FILE_CHARS)
            if len(chunk) > budget:
                break
            budget -= len(chunk)
            parts.append(f"### {name}\n{chunk}")
            sources.append({"type": "file", "name": name})
        recent = await self.service.recent_log(project, RECENT_ENTRIES)
        if recent:
            lines = [
                f"- {e.get('created_at', '')[:16]} {e.get('actor', '')}: {e.get('summary', '')}"
                for e in recent
            ]
            parts.append("### Recent activity\n" + "\n".join(lines))
        documents = await self.service.documents.list(project)
        if documents:
            titles = "\n".join(f"- {d.title} ({d.text_chars} characters)" for d in documents)
            parts.append("### Uploaded documents\n" + titles)
            seen_hits: set[tuple[str, int]] = set()
            for keyword in _keywords(question)[:4]:
                hits = await self.service.documents.search(project, ASSISTANT, keyword, 3)
                for hit in hits.get("hits", []):
                    marker = (hit["doc_id"], hit["offset"] // 500)
                    if marker in seen_hits or len(seen_hits) >= MAX_DOC_HITS:
                        continue
                    document = next((d for d in documents if d.doc_id == hit["doc_id"]), None)
                    if document is None:
                        continue
                    text = await self.service.documents.text(project, document)
                    start = max(0, hit["offset"] - HIT_RADIUS)
                    passage = " ".join(text[start : hit["offset"] + HIT_RADIUS].split())
                    if len(passage) > budget:
                        break
                    budget -= len(passage)
                    seen_hits.add(marker)
                    parts.append(f"### Passage from document '{document.title}'\n{passage}")
                    sources.append(
                        {"type": "document", "name": document.title, "doc_id": document.doc_id}
                    )
        return "\n\n".join(parts), sources
