from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .config import ADMIN_ROLE, CANONICAL_FILES, LOG_FILE, PERMISSIONS_FILE
from .markdown_engine import Document, parse, slugify


class Permission(StrEnum):
    NONE = "-"
    READ = "r"
    WRITE = "rw"
    WRITE_APPROVE = "rw-approve"

    @property
    def readable(self) -> bool:
        return self is not Permission.NONE

    @property
    def writable(self) -> bool:
        return self in (Permission.WRITE, Permission.WRITE_APPROVE)

    @classmethod
    def parse(cls, raw: str) -> Permission:
        value = raw.strip().casefold()
        for member in cls:
            if member.value == value:
                return member
        if value in ("w", "write"):
            return cls.WRITE
        if value in ("read", "ro"):
            return cls.READ
        return cls.NONE


@dataclass(slots=True, frozen=True)
class Target:
    file_name: str
    slug: str | None = None

    @classmethod
    def parse(cls, raw: str) -> Target:
        text = raw.strip().strip("`")
        if "#" in text:
            file_name, fragment = text.split("#", 1)
            return cls(file_name.strip(), slugify(fragment) or None)
        return cls(text, None)

    def matches(self, file_name: str, slug: str | None) -> bool:
        if self.file_name == "*":
            return True
        if self.file_name != file_name:
            return False
        return self.slug is None or self.slug == slug


@dataclass(slots=True)
class Policy:
    etag: str
    roles: tuple[str, ...]
    file_matrix: dict[str, dict[str, Permission]] = field(default_factory=dict)
    heading_overrides: dict[tuple[str, str], dict[str, Permission]] = field(default_factory=dict)
    watch: dict[str, tuple[Target, ...]] = field(default_factory=dict)
    cadence: dict[str, str] = field(default_factory=dict)
    agent_rules: tuple[str, ...] = ()
    file_guide: tuple[tuple[str, str], ...] = ()

    def permission(self, role: str, file_name: str, slug: str | None = None) -> Permission:
        if file_name == LOG_FILE:
            return Permission.READ
        if role == ADMIN_ROLE:
            return Permission.WRITE
        if file_name == PERMISSIONS_FILE:
            return Permission.READ if file_name in CANONICAL_FILES else Permission.NONE
        if role not in self.roles:
            return Permission.READ if file_name in CANONICAL_FILES else Permission.NONE
        if slug is not None:
            override = self.heading_overrides.get((file_name, slug), {}).get(role)
            if override is not None:
                return override
        row = self.file_matrix.get(file_name)
        if row is None:
            return Permission.READ
        return row.get(role, Permission.NONE)

    def can_read(self, role: str, file_name: str, slug: str | None = None) -> bool:
        return self.permission(role, file_name, slug).readable

    def write_permission(self, role: str, file_name: str, slug: str | None = None) -> Permission:
        permission = self.permission(role, file_name, slug)
        return permission if permission.writable else Permission.NONE

    def summary(self, role: str) -> dict[str, str]:
        return {name: self.permission(role, name).value for name in CANONICAL_FILES}

    def heading_summary(self, role: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for (file_name, slug), perms in self.heading_overrides.items():
            if role in perms:
                result[f"{file_name}#{slug}"] = perms[role].value
        return result

    def watches(self, role: str, file_name: str, slug: str | None) -> bool:
        targets = self.watch.get(role)
        if targets is None:
            return self.can_read(role, file_name)
        return any(target.matches(file_name, slug) for target in targets)

    def has_approvals(self) -> bool:
        for row in self.file_matrix.values():
            if Permission.WRITE_APPROVE in row.values():
                return True
        for perms in self.heading_overrides.values():
            if Permission.WRITE_APPROVE in perms.values():
                return True
        return False

    def writable_files(self, role: str) -> list[str]:
        return [name for name in CANONICAL_FILES if self.write_permission(role, name).writable]


def _table_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


def _bullets(body: str) -> tuple[str, ...]:
    items: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
        elif stripped[:2].rstrip(".").isdigit() and ". " in stripped[:4]:
            items.append(stripped.split(". ", 1)[1].strip())
    return tuple(items)


def _section_body(doc: Document, heading: str) -> str:
    section = doc.find(heading)
    return doc.body(section) if section else ""


def _parse_roles(body: str) -> tuple[str, ...]:
    for line in body.splitlines():
        stripped = line.strip().strip("`")
        if stripped and not stripped.startswith(("|", "-", "#")):
            return tuple(r.strip().casefold() for r in stripped.split(",") if r.strip())
    return ()


def _parse_file_matrix(body: str) -> dict[str, dict[str, Permission]]:
    rows = _table_rows(body)
    if not rows:
        return {}
    header = [cell.casefold() for cell in rows[0]]
    matrix: dict[str, dict[str, Permission]] = {}
    for row in rows[1:]:
        if not row:
            continue
        file_name = row[0].strip("`")
        matrix[file_name] = {
            header[i]: Permission.parse(row[i]) for i in range(1, min(len(header), len(row)))
        }
    return matrix


def _parse_overrides(body: str) -> dict[tuple[str, str], dict[str, Permission]]:
    overrides: dict[tuple[str, str], dict[str, Permission]] = {}
    for row in _table_rows(body)[1:]:
        if len(row) < 3:
            continue
        target = Target.parse(row[0])
        if target.slug is None:
            continue
        overrides.setdefault((target.file_name, target.slug), {})[row[1].casefold()] = (
            Permission.parse(row[2])
        )
    return overrides


def _parse_sync_policy(body: str) -> tuple[dict[str, tuple[Target, ...]], dict[str, str]]:
    watch: dict[str, tuple[Target, ...]] = {}
    cadence: dict[str, str] = {}
    for row in _table_rows(body)[1:]:
        if len(row) < 2:
            continue
        role = row[0].casefold()
        targets = tuple(Target.parse(item) for item in row[1].split(",") if item.strip())
        watch[role] = targets
        if len(row) > 2 and row[2]:
            cadence[role] = row[2]
    return watch, cadence


def _parse_guide(body: str) -> tuple[tuple[str, str], ...]:
    return tuple((row[0], row[1]) for row in _table_rows(body)[1:] if len(row) >= 2)


def parse_policy(text: str) -> Policy:
    doc = parse(text)
    roles = _parse_roles(_section_body(doc, "Roles"))
    policy = Policy(etag=doc.etag, roles=roles)
    policy.file_matrix = _parse_file_matrix(_section_body(doc, "File Matrix"))
    policy.heading_overrides = _parse_overrides(_section_body(doc, "Heading Overrides"))
    policy.watch, policy.cadence = _parse_sync_policy(_section_body(doc, "Sync Policy"))
    policy.agent_rules = _bullets(_section_body(doc, "Agent Rules"))
    policy.file_guide = _parse_guide(_section_body(doc, "File Guide"))
    return policy


def fail_closed_policy(etag: str = "none") -> Policy:
    return Policy(etag=etag, roles=(ADMIN_ROLE,))


def affected_roles(
    policy: Policy, file_name: str, slug: str | None, exclude: str | None = None
) -> list[str]:
    roles: list[str] = []
    for role in policy.roles:
        if role in (exclude, ADMIN_ROLE):
            continue
        targets = policy.watch.get(role)
        if targets and any(target.matches(file_name, slug) for target in targets):
            roles.append(role)
    return roles
