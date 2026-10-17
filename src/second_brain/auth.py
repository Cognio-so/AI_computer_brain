from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs

from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import ADMIN_ROLE, KEY_PREFIX, is_identifier
from .database import Database
from .keyvault import KeyVault
from .models import ApiKey, utcnow

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SECRET_BYTES = 32
SECRET_LENGTH = 43
KEY_ID_BYTES = 9
LAST_USED_THROTTLE_SECONDS = 60
PROTECTED_PREFIXES = ("/mcp", "/sse", "/messages", "/events")


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(slots=True, frozen=True)
class ParsedKey:
    project_id: str
    role: str
    agent_id: str
    secret: str
    raw: str


@dataclass(slots=True, frozen=True)
class Principal:
    project_id: str
    role: str
    agent_id: str
    key_id: str | None
    is_master: bool = False

    @property
    def is_admin(self) -> bool:
        return self.is_master or self.role == ADMIN_ROLE

    def actor(self, subagent_role: str | None = None) -> str:
        base = f"{self.role}/{self.agent_id}"
        return f"{base}[{subagent_role}]" if subagent_role else base

    def resolve_project(self, requested: str | None) -> str:
        if self.is_master:
            if not requested:
                raise AuthError(400, "project_id is required when using the master key")
            return requested
        if requested and requested != self.project_id:
            raise AuthError(403, "this key belongs to a different project")
        return self.project_id

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "role": self.role,
            "agent_id": self.agent_id,
            "key_id": self.key_id,
            "is_master": self.is_master,
        }


def _base62(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 62)
        chars.append(BASE62[remainder])
    return "".join(reversed(chars)).rjust(SECRET_LENGTH, "0")


def generate_secret() -> str:
    return _base62(secrets.token_bytes(SECRET_BYTES))


def generate_key_id() -> str:
    return _base62(secrets.token_bytes(KEY_ID_BYTES))[-12:]


def build_key(project_id: str, role: str, agent_id: str, secret: str) -> str:
    return f"{KEY_PREFIX}_{project_id}_{role}_{agent_id}_{secret}"


def parse_key(raw: str) -> ParsedKey | None:
    parts = raw.strip().split("_")
    if len(parts) != 5 or parts[0] != KEY_PREFIX:
        return None
    _, project_id, role, agent_id, secret = parts
    if not (is_identifier(project_id) and is_identifier(role) and is_identifier(agent_id)):
        return None
    if len(secret) < 20 or not secret.isalnum():
        return None
    return ParsedKey(project_id, role, agent_id, secret, raw.strip())


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def key_prefix(parsed: ParsedKey) -> str:
    return build_key(parsed.project_id, parsed.role, parsed.agent_id, parsed.secret[:4]) + "..."


def extract_token(headers: dict[str, str], query_string: str = "") -> str | None:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    header_key = headers.get("x-brain-key")
    if header_key:
        return header_key.strip()
    if query_string:
        values = parse_qs(query_string).get("key")
        if values and values[0].strip():
            return values[0].strip()
    return None


class RateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {}

    def _window(self, bucket: str) -> deque[float]:
        now = time.monotonic()
        window = self._windows.setdefault(bucket, deque())
        while window and now - window[0] > 60:
            window.popleft()
        return window

    def peek(self, bucket: str, limit_per_minute: int) -> bool:
        return len(self._window(bucket)) < limit_per_minute

    def allow(self, bucket: str, limit_per_minute: int) -> bool:
        window = self._window(bucket)
        if len(window) >= limit_per_minute:
            return False
        window.append(time.monotonic())
        return True


class Authenticator:
    def __init__(self, db: Database, master_key: str) -> None:
        self._db = db
        self._master_key = master_key
        self.vault = KeyVault(master_key)
        self._last_used_marks: dict[str, float] = {}

    def is_master(self, token: str) -> bool:
        return bool(self._master_key) and hmac.compare_digest(token, self._master_key)

    async def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthError(401, "missing API key")
        if self.is_master(token):
            return Principal("*", ADMIN_ROLE, "admin", None, is_master=True)
        parsed = parse_key(token)
        if parsed is None:
            raise AuthError(401, "malformed API key")
        digest = hash_key(parsed.raw)
        async with self._db.session() as session:
            record = await session.scalar(select(ApiKey).where(ApiKey.compound_key_hash == digest))
            if record is None or record.revoked_at is not None:
                raise AuthError(401, "unknown or revoked API key")
            consistent = (
                hmac.compare_digest(record.project_id, parsed.project_id)
                and hmac.compare_digest(record.department_role, parsed.role)
                and hmac.compare_digest(record.agent_id, parsed.agent_id)
            )
            if not consistent:
                raise AuthError(401, "API key does not match its record")
            await self._touch(session, record)
            return Principal(
                record.project_id, record.department_role, record.agent_id, record.key_id
            )

    async def _touch(self, session, record: ApiKey) -> None:
        now = time.monotonic()
        last = self._last_used_marks.get(record.key_id, 0.0)
        if now - last < LAST_USED_THROTTLE_SECONDS:
            return
        self._last_used_marks[record.key_id] = now
        record.last_used_at = utcnow()
        await session.commit()

    async def issue(
        self, project_id: str, role: str, agent_id: str, label: str | None = None
    ) -> tuple[ApiKey, str]:
        if not is_identifier(agent_id):
            raise AuthError(
                400,
                "agent_id must be 3-40 characters of a-z, 0-9 and hyphens, starting and ending with a letter or digit",
            )
        if not is_identifier(role):
            raise AuthError(
                400,
                "role must be 3-40 characters of a-z, 0-9 and hyphens, starting and ending with a letter or digit",
            )
        secret = generate_secret()
        plaintext = build_key(project_id, role, agent_id, secret)
        parsed = parse_key(plaintext)
        if parsed is None:
            raise AuthError(400, "could not build a valid key from these identifiers")
        record = ApiKey(
            key_id=generate_key_id(),
            project_id=project_id,
            department_role=role,
            agent_id=agent_id,
            label=label,
            key_prefix=key_prefix(parsed),
            compound_key_hash=hash_key(plaintext),
            secret_sealed=self.vault.seal(plaintext),
            created_at=utcnow(),
        )
        async with self._db.transaction() as session:
            session.add(record)
        return record, plaintext

    def reveal(self, record: ApiKey) -> str | None:
        return self.vault.open(record.secret_sealed)

    async def reseal(self, new_master_key: str) -> int:
        fresh = KeyVault(new_master_key)
        count = 0
        async with self._db.transaction() as session:
            result = await session.execute(select(ApiKey).where(ApiKey.secret_sealed.is_not(None)))
            for record in result.scalars():
                plaintext = self.vault.open(record.secret_sealed)
                record.secret_sealed = fresh.seal(plaintext) if plaintext else None
                count += 1 if plaintext else 0
        self.vault = fresh
        return count

    async def revoke(self, project_id: str, key_id: str) -> ApiKey | None:
        async with self._db.transaction() as session:
            record = await session.scalar(
                select(ApiKey).where(ApiKey.key_id == key_id, ApiKey.project_id == project_id)
            )
            if record is None:
                return None
            if record.revoked_at is None:
                record.revoked_at = utcnow()
            return record

    async def mark_synced(self, key_id: str | None, latest_log_id: int) -> None:
        if key_id is None:
            return
        async with self._db.transaction() as session:
            record = await session.get(ApiKey, key_id)
            if record is None:
                return
            record.last_sync_at = utcnow()
            if latest_log_id > record.last_sync_log_id:
                record.last_sync_log_id = latest_log_id

    async def unread_count(self, key_id: str | None, latest_log_id: int) -> int:
        if key_id is None:
            return 0
        async with self._db.session() as session:
            record = await session.get(ApiKey, key_id)
            if record is None:
                return 0
            return max(0, latest_log_id - record.last_sync_log_id)


class AgentAuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = extract_token(headers, scope.get("query_string", b"").decode("latin-1"))
        try:
            principal = await self.authenticator.authenticate(token)
        except AuthError as error:
            await _reject(send, error)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


async def _reject(send: Send, error: AuthError) -> None:
    body = json.dumps({"error": error.detail}).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if error.status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="ai-computer-brain"'))
    await send({"type": "http.response.start", "status": error.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
