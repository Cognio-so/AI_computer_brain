from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import asyncpg
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

log = logging.getLogger("brain.db_watch")

COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.is_nullable, c.column_default,
       c.character_maximum_length, c.numeric_precision, c.numeric_scale
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = $1 AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""
INDEXES_SQL = """
SELECT tablename, indexname
FROM pg_indexes
WHERE schemaname = $1
ORDER BY tablename, indexname
"""
VERSION_SQL = "SHOW server_version"
DATABASE_SQL = "SELECT current_database()"


def _column_type(row: asyncpg.Record) -> str:
    data_type = str(row["data_type"])
    if row["character_maximum_length"]:
        return f"{data_type}({row['character_maximum_length']})"
    if data_type == "numeric" and row["numeric_precision"]:
        return f"numeric({row['numeric_precision']},{row['numeric_scale'] or 0})"
    return data_type


async def introspect(dsn: str, schema: str = "public") -> dict[str, Any]:
    connection = await asyncpg.connect(dsn)
    try:
        columns = await connection.fetch(COLUMNS_SQL, schema)
        indexes = await connection.fetch(INDEXES_SQL, schema)
        version = await connection.fetchval(VERSION_SQL)
        database = await connection.fetchval(DATABASE_SQL)
    finally:
        await connection.close()
    tables: dict[str, dict[str, Any]] = {}
    for row in columns:
        table = tables.setdefault(
            str(row["table_name"]), {"name": str(row["table_name"]), "columns": [], "indexes": []}
        )
        table["columns"].append(
            {
                "name": str(row["column_name"]),
                "type": _column_type(row),
                "nullable": row["is_nullable"] == "YES",
                "default": row["column_default"],
            }
        )
    for row in indexes:
        table = tables.get(str(row["tablename"]))
        if table is not None:
            table["indexes"].append(str(row["indexname"]))
    return {
        "engine": {
            "name": str(database),
            "engine": "PostgreSQL",
            "version": str(version).split()[0],
            "purpose": "",
        },
        "tables": [tables[name] for name in sorted(tables)],
    }


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def describe_change(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    current_tables = {t["name"]: t for t in current.get("tables", [])}
    if previous is None:
        return f"schema watcher: {len(current_tables)} tables imported"
    previous_tables = {t["name"]: t for t in previous.get("tables", [])}
    added = sorted(set(current_tables) - set(previous_tables))
    removed = sorted(set(previous_tables) - set(current_tables))
    changed = sorted(
        name
        for name in set(current_tables) & set(previous_tables)
        if fingerprint(current_tables[name]) != fingerprint(previous_tables[name])
    )
    parts = []
    if added:
        parts.append("added " + ", ".join(added[:5]))
    if removed:
        parts.append("removed " + ", ".join(removed[:5]))
    if changed:
        parts.append("changed " + ", ".join(changed[:5]))
    return "schema watcher: " + ("; ".join(parts) or "schema changed")


async def push(endpoint: str, key: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
    async with streamable_http_client(endpoint, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "sync_db_schema", {"schema_payload": payload, "compact_summary": summary}
            )
            if result.structured_content:
                return dict(result.structured_content)
            return {"text": [getattr(block, "text", "") for block in result.content]}


async def watch(dsn: str, endpoint: str, key: str, interval: int, schema: str, once: bool) -> None:
    previous: dict[str, Any] | None = None
    last_digest: str | None = None
    while True:
        try:
            payload = await introspect(dsn, schema)
            digest = fingerprint(payload)
            if digest != last_digest:
                summary = describe_change(previous, payload)
                result = await push(endpoint, key, payload, summary)
                log.info("pushed schema (%s): %s", summary, result.get("status") or result)
                previous, last_digest = payload, digest
            else:
                log.debug("schema unchanged")
        except Exception:
            log.exception("schema check failed")
        if once:
            return
        await asyncio.sleep(interval)
