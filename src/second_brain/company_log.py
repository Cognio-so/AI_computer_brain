from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from .config import LOG_FILE
from .database import Database
from .locks import LockRegistry
from .markdown_engine import parse, replace_section
from .models import MutationLog, target_file, target_slug, utcnow
from .rbac import Policy
from .vault import Vault

HOT_HEADING = "Hot Log"
FINDINGS_HEADING = "Findings"
EMPTY_MARKER = "- (no entries yet)"
FINDINGS_EMPTY = "- (no findings yet)"
ENTRY_RE = re.compile(r"^- #(\d+) ")
MAX_SCAN = 500


def short_target(uri: str) -> str:
    file_name = target_file(uri)
    slug = target_slug(uri)
    return f"{file_name}#{slug}" if slug else file_name


def format_entry(entry: MutationLog) -> str:
    stamp = (entry.created_at or utcnow()).strftime("%Y-%m-%dT%H:%MZ")
    etags = ""
    if entry.new_etag:
        etags = f" ({entry.previous_etag or '-'}→{entry.new_etag})"
    return f"- #{entry.log_id} {stamp} {entry.actor} → {short_target(entry.target_uri)} — {entry.compact_summary}{etags}"


def format_finding(entry: MutationLog) -> str:
    stamp = (entry.created_at or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
    summary = entry.compact_summary.removeprefix("[finding] ")
    line = f"- {stamp} · {entry.actor} · {summary}"
    if entry.affects_list:
        line += " · affects: " + ", ".join(entry.affects_list)
    return line


def _month(value: datetime | None) -> str:
    return (value or utcnow()).strftime("%Y-%m")


class CompanyLog:
    def __init__(self, db: Database, vault: Vault, locks: LockRegistry, hot_size: int) -> None:
        self._db = db
        self._vault = vault
        self._locks = locks
        self._hot_size = hot_size

    @property
    def project_id(self) -> str:
        return self._vault.project_id

    def build_entry(
        self,
        *,
        event_type: str,
        agent_id: str,
        department: str,
        target_uri: str,
        compact_summary: str,
        importance: str = "normal",
        subagent_role: str | None = None,
        previous_etag: str | None = None,
        new_etag: str | None = None,
        commit_sha: str | None = None,
        affects: list[str] | None = None,
    ) -> MutationLog:
        return MutationLog(
            project_id=self.project_id,
            event_type=event_type,
            agent_id=agent_id,
            department=department,
            subagent_role=subagent_role,
            target_uri=target_uri,
            compact_summary=compact_summary,
            importance=importance,
            previous_etag=previous_etag,
            new_etag=new_etag,
            commit_sha=commit_sha,
            affects=",".join(dict.fromkeys(affects)) if affects else None,
            created_at=utcnow(),
        )

    async def record(self, rewrite: bool = True, **fields) -> MutationLog:
        entry = self.build_entry(**fields)
        async with self._db.transaction() as session:
            session.add(entry)
            await session.flush()
        if rewrite:
            await self.rewrite_hot_log()
        return entry

    async def latest_id(self) -> int:
        async with self._db.session() as session:
            value = await session.scalar(
                select(func.max(MutationLog.log_id)).where(
                    MutationLog.project_id == self.project_id
                )
            )
        return int(value or 0)

    async def recent(self, limit: int) -> list[MutationLog]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(MutationLog)
                .where(MutationLog.project_id == self.project_id)
                .order_by(MutationLog.log_id.desc())
                .limit(limit)
            )
            return list(rows)

    async def entries(self, since: int, limit: int) -> tuple[list[MutationLog], int]:
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(MutationLog)
                    .where(MutationLog.project_id == self.project_id, MutationLog.log_id > since)
                    .order_by(MutationLog.log_id.asc())
                    .limit(limit)
                )
            )
        latest = rows[-1].log_id if rows else await self.latest_id()
        return rows, max(latest, since)

    async def delta(
        self,
        since: int,
        limit: int,
        policy: Policy | None = None,
        role: str | None = None,
        include_low: bool = False,
    ) -> tuple[list[MutationLog], int]:
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(MutationLog)
                    .where(MutationLog.project_id == self.project_id, MutationLog.log_id > since)
                    .order_by(MutationLog.log_id.asc())
                    .limit(MAX_SCAN)
                )
            )
        if not rows:
            return [], max(since, await self.latest_id())
        selected: list[MutationLog] = []
        latest_seen = since
        for row in rows:
            latest_seen = row.log_id
            if row.importance == "low" and not include_low:
                continue
            if policy is not None and role is not None and not self._relevant(policy, role, row):
                continue
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected, latest_seen

    @staticmethod
    def affects_role(role: str, row: MutationLog) -> bool:
        affected = row.affects_list
        return role in affected or "all" in affected

    @classmethod
    def _relevant(cls, policy: Policy, role: str, row: MutationLog) -> bool:
        if cls.affects_role(role, row):
            return True
        if not row.target_uri.startswith("brain://"):
            return True
        return policy.watches(role, row.file_name, row.slug)

    async def entries_for_day(self, day: date) -> list[MutationLog]:
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        async with self._db.session() as session:
            rows = await session.scalars(
                select(MutationLog)
                .where(
                    MutationLog.project_id == self.project_id,
                    MutationLog.created_at >= start,
                    MutationLog.created_at < end,
                )
                .order_by(MutationLog.log_id.asc())
            )
            return list(rows)

    async def get(self, log_id: int) -> MutationLog | None:
        async with self._db.session() as session:
            entry = await session.get(MutationLog, log_id)
            if entry is None or entry.project_id != self.project_id:
                return None
            return entry

    async def attach_commit(self, log_ids: list[int], commit_sha: str) -> None:
        if not log_ids:
            return
        async with self._db.transaction() as session:
            rows = await session.scalars(select(MutationLog).where(MutationLog.log_id.in_(log_ids)))
            for row in rows:
                row.commit_sha = commit_sha

    async def rewrite_hot_log(self) -> None:
        async with self._locks.hold(self.project_id, LOG_FILE):
            entries = await self.recent(self._hot_size)
            text = await self._vault.read(LOG_FILE)
            doc = parse(text)
            section = doc.find(HOT_HEADING)
            current_ids: set[int] = set()
            if section is not None:
                for line in doc.body(section).splitlines():
                    match = ENTRY_RE.match(line.strip())
                    if match:
                        current_ids.add(int(match.group(1)))
            kept_ids = {entry.log_id for entry in entries}
            dropped = sorted(current_ids - kept_ids)
            if dropped:
                await self._archive(dropped)
            body = "\n".join(format_entry(entry) for entry in entries) or EMPTY_MARKER
            if section is None:
                new_text = text.rstrip("\n") + f"\n\n## {HOT_HEADING}\n\n{body}\n"
            else:
                new_text, _ = replace_section(text, HOT_HEADING, body)
            await self._vault.write(LOG_FILE, new_text)

    async def latest_findings(self, limit: int) -> list[MutationLog]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(MutationLog)
                .where(
                    MutationLog.project_id == self.project_id,
                    MutationLog.event_type == "activity_finding",
                )
                .order_by(MutationLog.log_id.desc())
                .limit(limit)
            )
            return list(rows)

    async def rewrite_findings(self, limit: int) -> None:
        findings = await self.latest_findings(limit)
        body = "\n".join(format_finding(entry) for entry in findings) or FINDINGS_EMPTY
        async with self._locks.hold(self.project_id, LOG_FILE):
            text = await self._vault.read(LOG_FILE)
            doc = parse(text)
            if doc.find(FINDINGS_HEADING) is None:
                hot = doc.find(HOT_HEADING)
                insert_at = hot.end if hot is not None else len(doc.lines)
                lines = list(doc.lines)
                lines[insert_at:insert_at] = [
                    "",
                    f"## {FINDINGS_HEADING}",
                    "",
                    *body.splitlines(),
                    "",
                ]
                new_text = "\n".join(lines).rstrip("\n") + "\n"
            else:
                new_text, _ = replace_section(text, FINDINGS_HEADING, body)
            await self._vault.write(LOG_FILE, new_text)

    async def recent_activity(self, hours: int) -> list[MutationLog]:
        since = utcnow() - timedelta(hours=hours)
        async with self._db.session() as session:
            rows = await session.scalars(
                select(MutationLog)
                .where(
                    MutationLog.project_id == self.project_id,
                    MutationLog.created_at >= since,
                    MutationLog.event_type.like("activity_%"),
                )
                .order_by(MutationLog.log_id.desc())
            )
            return list(rows)

    async def _archive(self, log_ids: list[int]) -> None:
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(MutationLog)
                    .where(MutationLog.log_id.in_(log_ids))
                    .order_by(MutationLog.log_id.asc())
                )
            )
        by_month: dict[str, list[str]] = {}
        for row in rows:
            by_month.setdefault(_month(row.created_at), []).append(format_entry(row))
        for month, lines in by_month.items():
            await self._vault.append_archive(month, lines)
