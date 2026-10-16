from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ResourceUpdated, ToolsListChanged
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ListToolsResult
from pydantic import Field
from redis.asyncio import Redis

from . import __version__
from .auth import AgentAuthMiddleware, Authenticator, Principal
from .config import PERMISSIONS_FILE, Settings, get_settings
from .database import Database
from .documents import DocumentError
from .hub import Event, build_hub
from .locks import build_lock_registry
from .models import Project, brain_uri
from .service import BrainError, BrainService, WriteResult
from .vault import VaultManager
from .web_ui import admin, agent, brain_error_handler, pages, register_pages
from .webhooks import router as webhooks_router

log = logging.getLogger("brain.server")

SESSION_TOUCH_SECONDS = 30
ALL_TOOLS = (
    "whoami",
    "get_brain_index",
    "read_brain_section",
    "read_brain_file",
    "update_brain_section",
    "get_company_log_delta",
    "log_agent_activity",
    "get_daily_digest",
    "list_documents",
    "search_documents",
    "read_document",
    "sync_db_schema",
    "get_proposal",
    "list_projects",
)

ProjectArg = Annotated[
    str | None,
    Field(
        description=(
            "Project ID. Leave empty: your key already selects the project. "
            "Only the master key must pass it."
        )
    ),
]
FileArg = Annotated[
    str,
    Field(
        description=(
            "One of DESIGN_SYSTEM.md, TECH_AND_INFRA.md, DB_SCHEMA_STATE.md, COMPANY_LOG.md, "
            "ACCESS_PERMISSIONS.md, COMPANY_PULSE.md, AGENT_CHANGELOG.md."
        )
    ),
]
HeadingArg = Annotated[
    str,
    Field(
        description=(
            "Heading text or slug of a ## or ### section, e.g. 'Pricing Tiers' or "
            "'pricing-tiers'. For a new ### under a parent use 'Parent > Child'."
        )
    ),
]
EtagArg = Annotated[
    str | None,
    Field(
        description=(
            "The etag you received when you last read this section. Lets the server answer "
            "NOT_MODIFIED or detect conflicts."
        )
    ),
]


def assemble_service(settings: Settings) -> BrainService:
    settings.ensure_directories()
    db = Database(settings.database_url, settings.sql_dir)
    redis = Redis.from_url(settings.redis_url) if settings.redis_enabled else None
    return BrainService(
        settings,
        db,
        build_hub(redis),
        build_lock_registry(redis),
        VaultManager(settings),
        Authenticator(db, settings.admin_master_key),
    )


async def build_service(settings: Settings) -> BrainService:
    service = assemble_service(settings)
    await service.db.init()
    return service


def principal_of(ctx: Context) -> Principal:
    request = ctx.request_context.request
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None:
        raise BrainError(401, "UNAUTHENTICATED", "no API key on this connection")
    return principal


def _request_principal(ctx: ServerRequestContext[Any, Any]) -> Principal | None:
    request = getattr(ctx, "request", None)
    return getattr(getattr(request, "state", None), "principal", None)


def _session_id(ctx: ServerRequestContext[Any, Any]) -> str | None:
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    session_id = headers.get("mcp-session-id")
    if session_id:
        return f"mcp-{session_id}"
    query = getattr(request, "query_params", None)
    if query is not None and query.get("session_id"):
        return f"sse-{query.get('session_id')}"
    return None


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


def _filter_tools(result: Any, allowed: set[str]) -> Any:
    if isinstance(result, ListToolsResult):
        return result.model_copy(update={"tools": [t for t in result.tools if t.name in allowed]})
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        filtered = dict(result)
        filtered["tools"] = [t for t in result["tools"] if _tool_name(t) in allowed]
        return filtered
    return result


class BrainMiddleware:
    def __init__(self, service: BrainService) -> None:
        self.service = service
        self._touched: dict[str, float] = {}

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: Callable[[ServerRequestContext[Any, Any]], Awaitable[Any]],
    ) -> Any:
        principal = _request_principal(ctx)
        if principal is not None:
            await self._touch(ctx, principal)
        result = await call_next(ctx)
        if ctx.method == "tools/list" and principal is not None:
            allowed = await self._allowed_tools(principal)
            if allowed is not None:
                result = _filter_tools(result, allowed)
        return result

    async def _allowed_tools(self, principal: Principal) -> set[str] | None:
        if principal.is_master:
            return set(ALL_TOOLS)
        try:
            project = await self.service.get_project(principal.project_id)
            policy = await self.service.policy(project)
        except BrainError:
            return None
        return set(self.service.tool_names(principal, policy))

    async def _touch(self, ctx: ServerRequestContext[Any, Any], principal: Principal) -> None:
        session_id = _session_id(ctx)
        if session_id is None:
            return
        now = time.monotonic()
        if now - self._touched.get(session_id, 0.0) < SESSION_TOUCH_SECONDS:
            return
        self._touched[session_id] = now
        if len(self._touched) > 5000:
            for key in [k for k, v in self._touched.items() if now - v > 3600]:
                self._touched.pop(key, None)
        client_info = None
        params = getattr(ctx.session, "client_params", None)
        info = getattr(params, "client_info", None)
        if info is not None:
            name = getattr(info, "name", "")
            version = getattr(info, "version", "")
            client_info = f"{name} {version}".strip() or None
        transport = "mcp-sse" if session_id.startswith("sse-") else "mcp-http"
        try:
            await self.service.touch_session(principal, transport, session_id, client_info)
        except Exception:
            log.exception("could not record agent session")


def create_mcp(service: BrainService) -> tuple[MCPServer, InMemorySubscriptionBus]:
    bus = InMemorySubscriptionBus()
    mcp = MCPServer(
        "ai-computer-brain",
        title="AI Computer Brain",
        description="Shared, Git-backed memory that keeps every agent in a project in sync.",
        instructions=service.protocol_text(),
        version=__version__,
        middleware=[BrainMiddleware(service)],
        subscriptions=bus,
    )

    async def run(
        ctx: Context,
        project_id: str | None,
        operation: Callable[[Principal, Project], Awaitable[Any]],
        banner: bool = True,
    ) -> dict[str, Any]:
        try:
            principal = principal_of(ctx)
            project = await service.resolve(principal, project_id)
            result = await operation(principal, project)
            payload = result.to_dict() if isinstance(result, WriteResult) else dict(result)
            if banner:
                sync = await service.sync_banner(principal, project)
                if sync:
                    payload["sync"] = sync
            return payload
        except BrainError as error:
            return error.to_dict()

    @mcp.tool(
        name="whoami",
        description=(
            "Call this first in every new session. Returns your project, role, what you may read "
            "and write, the rules for keeping the brain current, the file guide (what to update "
            "where), your watch list, your changelog section, what you logged today and the latest "
            "log id to start syncing from."
        ),
    )
    async def whoami(ctx: Context, project_id: ProjectArg = None) -> dict[str, Any]:
        return await run(ctx, project_id, service.whoami, banner=False)

    @mcp.tool(
        name="get_brain_index",
        description=(
            "Table of contents of the brain files: every ## and ### heading with its 8-char etag, "
            "plus a file-level etag. About 150 tokens. Compare etags with the ones you cached to "
            "see what changed."
        ),
    )
    async def get_brain_index(ctx: Context, project_id: ProjectArg = None) -> dict[str, Any]:
        return await run(ctx, project_id, lambda p, pr: _wrap(service.index(p, pr), "files"))

    @mcp.tool(
        name="read_brain_section",
        description=(
            "Read one heading of a brain file. Always pass known_etag from your last read: if "
            "nothing changed you get NOT_MODIFIED for almost no tokens. Prefer this over "
            "read_brain_file."
        ),
    )
    async def read_brain_section(
        ctx: Context,
        file_name: FileArg,
        heading: HeadingArg,
        known_etag: EtagArg = None,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: service.read_section(p, pr, file_name, heading, known_etag),
        )

    @mcp.tool(
        name="read_brain_file",
        description=(
            "Read a whole brain file with its section index. Pass known_etag to get NOT_MODIFIED "
            "when unchanged. Use read_brain_section when you only need one heading."
        ),
    )
    async def read_brain_file(
        ctx: Context, file_name: FileArg, known_etag: EtagArg = None, project_id: ProjectArg = None
    ) -> dict[str, Any]:
        return await run(
            ctx, project_id, lambda p, pr: service.read_file(p, pr, file_name, known_etag)
        )

    @mcp.tool(
        name="update_brain_section",
        description=(
            "Call immediately after you change anything other agents must know: a price, a "
            "shipped feature, a schema, a brand token, an endpoint, a decision. Writes under one "
            "heading only. compact_summary (max 160 chars) is written for the other agents. Pass "
            "expected_etag from your last read; on CONFLICT re-read the returned current_content, "
            "merge, retry once. Returns PENDING_APPROVAL when your role needs an admin to approve "
            "writes to that section. Departments that watch the section are recorded in affects "
            "and see the change flagged as affecting them."
        ),
    )
    async def update_brain_section(
        ctx: Context,
        file_name: FileArg,
        heading: HeadingArg,
        patch_content: Annotated[
            str,
            Field(
                description=(
                    "Markdown for the section body. With mode=replace it replaces the body; "
                    "with append it is added at the end; with create it becomes a new section."
                )
            ),
        ],
        compact_summary: Annotated[
            str,
            Field(
                description="One line, max 160 chars, telling other agents what changed and why."
            ),
        ],
        mode: Annotated[
            str, Field(description="replace (default), append, create or delete (admin only).")
        ] = "replace",
        expected_etag: EtagArg = None,
        subagent_role: Annotated[
            str | None,
            Field(
                description=(
                    "Label for a sub-agent making this write on behalf of the parent agent, "
                    "e.g. 'ui-subagent'."
                )
            ),
        ] = None,
        importance: Annotated[
            str,
            Field(
                description=(
                    "low, normal (default) or high. Low entries are hidden from other agents' "
                    "deltas unless they ask for them."
                )
            ),
        ] = "normal",
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: service.update_section(
                p,
                pr,
                file_name,
                heading,
                patch_content,
                compact_summary,
                mode,
                expected_etag,
                subagent_role,
                importance,
            ),
        )

    @mcp.tool(
        name="get_company_log_delta",
        description=(
            "Call at the start of every turn before doing work, with the latest_id you saw last "
            "time. Returns what other agents and humans changed since, filtered to your watch "
            "list plus anything marked as affecting your department. Entries with relevance "
            "affects_you are work for you: adjust your output and log the adjustment. About 20 "
            "tokens per entry, 0 when nothing changed."
        ),
    )
    async def get_company_log_delta(
        ctx: Context,
        since_entry_id: Annotated[
            int, Field(description="The latest_id from your previous call, 0 on first use.", ge=0)
        ] = 0,
        limit: Annotated[int, Field(description="Maximum entries to return.", ge=1, le=200)] = 50,
        include_low: Annotated[
            bool, Field(description="Also return low-importance entries.")
        ] = False,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: service.log_delta(p, pr, since_entry_id, limit, include_low),
            banner=False,
        )

    @mcp.tool(
        name="log_agent_activity",
        description=(
            "Call after work other departments must know about: a change they rely on, something "
            "shipped, something new found (a feature, service, customer need or problem), a "
            "decision, a blocker, a new capability. Routine steps stay in your own memory. Also "
            "call it before you finish a session with your findings of the day. Writes a dated UTC "
            "entry under your own department in AGENT_CHANGELOG.md, surfaces findings in "
            "COMPANY_LOG.md and refreshes the Daily Digest; an entry equal to one logged in the "
            "last 24 hours returns DUPLICATE and is not written twice. kind is task, finding, "
            "decision or blocker. affects lists the departments that must adjust, e.g. "
            "['marketing'] when design changed a style the slides use; they see it flagged "
            "affects_you in their next delta. You can never write another department's section."
        ),
    )
    async def log_agent_activity(
        ctx: Context,
        summary: Annotated[
            str, Field(description="One line, max 160 chars: what you did or found.")
        ],
        kind: Annotated[
            str, Field(description="task (default), finding, decision or blocker.")
        ] = "task",
        affects: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Departments that must act on this, e.g. ['marketing', 'engineering'] or "
                    "['all']. Leave empty when nobody else is concerned."
                )
            ),
        ] = None,
        details: Annotated[
            str | None,
            Field(description="Optional second line with specifics, max 300 chars."),
        ] = None,
        importance: Annotated[str, Field(description="low, normal (default) or high.")] = "normal",
        subagent_role: Annotated[
            str | None, Field(description="Label for a sub-agent logging on behalf of the parent.")
        ] = None,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: service.log_activity(
                p, pr, summary, kind, affects, details, importance, subagent_role
            ),
        )

    @mcp.tool(
        name="get_daily_digest",
        description=(
            "Everything that happened in the project on one day across all departments: tasks, "
            "findings, decisions, blockers, brain updates, schema syncs and human commits, with "
            "who did it, when, and which departments it affects. Defaults to today (UTC)."
        ),
    )
    async def get_daily_digest(
        ctx: Context,
        date: Annotated[
            str | None, Field(description="ISO date such as 2026-10-15. Empty means today.")
        ] = None,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx, project_id, lambda p, pr: service.daily_digest(p, pr, date), banner=False
        )

    @mcp.tool(
        name="list_documents",
        description=(
            "Documents the admin uploaded as your knowledge base (specs, SOPs, contracts, "
            "research), limited to the ones shared with your agent. Returns id, title and size; "
            "read one with read_document or find passages with search_documents."
        ),
    )
    async def list_documents(ctx: Context, project_id: ProjectArg = None) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: _wrap(
                service.documents.visible(pr, p),
                "documents",
                lambda docs: [d.brief() for d in docs],
            ),
            banner=False,
        )

    @mcp.tool(
        name="search_documents",
        description=(
            "Find passages in the documents shared with you. Pass a few keywords; every hit "
            "returns the document id, a snippet and the offset to continue reading from with "
            "read_document."
        ),
    )
    async def search_documents(
        ctx: Context,
        query: Annotated[str, Field(description="Keywords to look for, all must appear.")],
        limit: Annotated[int, Field(description="Maximum hits.", ge=1, le=30)] = 10,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: _documents_call(service.documents.search(pr, p, query, limit)),
            banner=False,
        )

    @mcp.tool(
        name="read_document",
        description=(
            "Read a document shared with you, in chunks so it fits your context: pass offset "
            "(0 first) and max_chars (default 6000). The answer carries next_offset until the "
            "end; stop when it is null."
        ),
    )
    async def read_document(
        ctx: Context,
        doc_id: Annotated[
            str, Field(description="The id from list_documents or search_documents.")
        ],
        offset: Annotated[int, Field(description="Character offset to start from.", ge=0)] = 0,
        max_chars: Annotated[
            int, Field(description="Characters to return, 200 to 20000.", ge=200, le=20000)
        ] = 6000,
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: _documents_call(service.documents.read(pr, p, doc_id, offset, max_chars)),
            banner=False,
        )

    @mcp.tool(
        name="sync_db_schema",
        description=(
            "Call after running a migration, or whenever the live database schema differs from "
            "DB_SCHEMA_STATE.md. Pass the schema as JSON: {engine?: {name, engine, version, "
            "purpose}, tables: [{name, columns: [{name, type, nullable, default, notes}], "
            "indexes: [..], notes}]}. Only tables that changed are rewritten and a Migration Log "
            "line is added."
        ),
    )
    async def sync_db_schema(
        ctx: Context,
        schema_payload: Annotated[
            dict[str, Any], Field(description="Normalised schema object with a tables list.")
        ],
        compact_summary: Annotated[
            str, Field(description="One line describing the schema change for other agents.")
        ],
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: service.sync_db_schema(p, pr, schema_payload, compact_summary),
        )

    @mcp.tool(
        name="get_proposal",
        description=(
            "Status of a write that returned PENDING_APPROVAL: pending, approved, rejected or "
            "superseded, with the reviewer's note."
        ),
    )
    async def get_proposal(
        ctx: Context,
        proposal_id: Annotated[
            str, Field(description="The proposal_id returned by update_brain_section.")
        ],
        project_id: ProjectArg = None,
    ) -> dict[str, Any]:
        return await run(
            ctx,
            project_id,
            lambda p, pr: _wrap(
                service.get_proposal(p, pr, proposal_id), None, lambda x: x.to_dict()
            ),
            banner=False,
        )

    @mcp.tool(
        name="list_projects", description="List every project on this server. Master key only."
    )
    async def list_projects(ctx: Context) -> dict[str, Any]:
        try:
            principal = principal_of(ctx)
        except BrainError as error:
            return error.to_dict()
        if not principal.is_master:
            return BrainError(
                403, "FORBIDDEN", "the master key is required to list projects"
            ).to_dict()
        return {
            "projects": [await service.project_overview(p) for p in await service.list_projects()]
        }

    @mcp.resource(
        "brain://{project_id}/index",
        name="brain_index",
        description="Section index with etags for all brain files.",
        mime_type="application/json",
    )
    async def resource_index(project_id: str, ctx: Context) -> str:
        return _json(
            await run(
                ctx, project_id, lambda p, pr: _wrap(service.index(p, pr), "files"), banner=False
            )
        )

    @mcp.resource(
        "brain://{project_id}/{file_name}",
        name="brain_file",
        description="One of the canonical Markdown files.",
        mime_type="text/markdown",
    )
    async def resource_file(project_id: str, file_name: str, ctx: Context) -> str:
        result = await run(
            ctx, project_id, lambda p, pr: service.read_file(p, pr, file_name, None), banner=False
        )
        return str(result.get("content") or _json(result))

    @mcp.resource(
        "brain://{project_id}/{file_name}/sections/{slug}",
        name="brain_section",
        description="One heading of a canonical file.",
        mime_type="text/markdown",
    )
    async def resource_section(project_id: str, file_name: str, slug: str, ctx: Context) -> str:
        result = await run(
            ctx,
            project_id,
            lambda p, pr: service.read_section(p, pr, file_name, slug, None),
            banner=False,
        )
        return str(result.get("content") or _json(result))

    @mcp.prompt(
        name="brain_bootstrap",
        description=(
            "Operating rules for this agent: how and when to read and write the shared brain, "
            "tailored to its role."
        ),
    )
    async def brain_bootstrap(ctx: Context) -> str:
        try:
            principal = principal_of(ctx)
            if principal.is_master:
                return service.protocol_text()
            project = await service.get_project(principal.project_id)
            return service.protocol_text(principal, await service.policy(project))
        except BrainError:
            return service.protocol_text()

    return mcp, bus


async def _documents_call(awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await awaitable
    except DocumentError as error:
        return {"status": "ERROR", "error": error.detail, "code": error.status}


async def _wrap(
    awaitable: Awaitable[Any], key: str | None, transform: Callable[[Any], Any] | None = None
) -> dict[str, Any]:
    value = await awaitable
    if transform is not None:
        value = transform(value)
    if key is None:
        return value if isinstance(value, dict) else {"result": value}
    return {key: value}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def notification_forwarder(bus: InMemorySubscriptionBus) -> Callable[[Event], Awaitable[None]]:
    async def forward(event: Event) -> None:
        uris = [brain_uri(event.project_id, "index")]
        if event.file:
            file_uri = brain_uri(event.project_id, event.file)
            uris.append(file_uri)
            if event.slug:
                uris.append(f"{file_uri}/sections/{event.slug}")
        for uri in uris:
            await bus.publish(ResourceUpdated(uri=uri))
        if event.file == PERMISSIONS_FILE:
            await bus.publish(ToolsListChanged())

    return forward


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    service = assemble_service(settings)
    mcp, bus = create_mcp(service)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    streamable = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security)
    legacy = mcp.sse_app(sse_path="/sse", message_path="/messages/", transport_security=security)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.db.init()
        await service.start()
        service.hub.add_listener("*", notification_forwarder(bus))
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await service.stop()
            await service.db.close()

    app = FastAPI(
        title="AI Computer Brain",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.brain = service
    app.add_exception_handler(BrainError, brain_error_handler)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["ETag", "Mcp-Session-Id"],
        )
    app.include_router(pages)
    register_pages(app, settings)
    app.include_router(admin)
    app.include_router(agent)
    app.include_router(webhooks_router)
    app.router.routes.extend(streamable.routes)
    app.router.routes.extend(legacy.routes)
    app.add_middleware(AgentAuthMiddleware, authenticator=service.auth)
    return app
