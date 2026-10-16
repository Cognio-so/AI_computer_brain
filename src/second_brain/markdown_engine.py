from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
MAX_SECTION_LEVEL = 3


class WriteMode(StrEnum):
    REPLACE = "replace"
    APPEND = "append"
    CREATE = "create"
    DELETE = "delete"


class SectionNotFound(LookupError):
    pass


class SectionExists(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class Section:
    heading: str
    level: int
    slug: str
    start: int
    end: int
    etag: str

    @property
    def body_start(self) -> int:
        return self.start + 1

    @property
    def path(self) -> str:
        return f"#{self.slug}"


@dataclass(slots=True, frozen=True)
class Document:
    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    etag: str
    title: str | None

    @property
    def text(self) -> str:
        return render(self.lines)

    def find(self, heading_or_slug: str) -> Section | None:
        wanted = heading_or_slug.strip().lstrip("#")
        wanted_slug = slugify(wanted)
        for section in self.sections:
            if section.slug == wanted_slug or section.heading.casefold() == wanted.casefold():
                return section
        return None

    def require(self, heading_or_slug: str) -> Section:
        section = self.find(heading_or_slug)
        if section is None:
            raise SectionNotFound(heading_or_slug)
        return section

    def body(self, section: Section) -> str:
        return render(self.lines[section.body_start : section.end]).strip("\n")

    def children(self, section: Section) -> tuple[Section, ...]:
        return tuple(
            child
            for child in self.sections
            if child.level > section.level and section.start < child.start < section.end
        )

    def parent(self, section: Section) -> Section | None:
        if section.level == 2:
            return None
        for candidate in reversed(self.sections):
            if candidate.level < section.level and candidate.start < section.start:
                return candidate
        return None

    def index(self) -> list[dict[str, object]]:
        return [
            {"heading": s.heading, "level": s.level, "slug": s.slug, "etag": s.etag}
            for s in self.sections
        ]


def slugify(heading: str) -> str:
    return SLUG_STRIP_RE.sub("-", heading.strip().casefold()).strip("-")


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def normalize(lines: tuple[str, ...] | list[str]) -> str:
    stripped = [line.rstrip() for line in lines]
    while stripped and not stripped[-1]:
        stripped.pop()
    while stripped and not stripped[0]:
        stripped.pop(0)
    return "\n".join(stripped)


def render(lines: tuple[str, ...] | list[str]) -> str:
    text = "\n".join(lines)
    return text if text.endswith("\n") else text + "\n"


def split_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for number, line in enumerate(lines):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
                continue
            if marker[0] == fence:
                fence = None
                continue
        if fence is not None:
            continue
        match = HEADING_RE.match(line)
        if match:
            found.append((number, len(match.group(1)), match.group(2).strip()))
    return found


def parse(text: str) -> Document:
    lines = split_lines(text)
    headings = _headings(lines)
    title = next((h for n, level, h in headings if level == 1), None)
    sections: list[Section] = []
    for position, (start, level, heading) in enumerate(headings):
        if level < 2 or level > MAX_SECTION_LEVEL:
            continue
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        etag = short_hash(normalize(lines[start + 1 : end]))
        sections.append(Section(heading, level, slugify(heading), start, end, etag))
    if sections:
        file_etag = short_hash("\n".join(s.etag for s in sections))
    else:
        file_etag = short_hash(normalize(lines))
    return Document(tuple(lines), tuple(sections), file_etag, title)


def _body_lines(content: str) -> list[str]:
    body = split_lines(content.strip("\n"))
    return [line.rstrip() for line in body]


def _splice(lines: tuple[str, ...], start: int, end: int, replacement: list[str]) -> list[str]:
    merged = list(lines[:start]) + replacement + list(lines[end:])
    cleaned: list[str] = []
    for line in merged:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def _section_block(
    heading_line: str, body: list[str], children: list[str] | None = None
) -> list[str]:
    block = [heading_line, ""]
    if body:
        block.extend(body)
        block.append("")
    if children:
        block.extend(children)
        if block[-1] != "":
            block.append("")
    return block


def replace_section(text: str, heading: str, content: str) -> tuple[str, Section]:
    doc = parse(text)
    section = doc.require(heading)
    children = doc.children(section)
    own_end = children[0].start if children else section.end
    nested = list(doc.lines[own_end : section.end]) if children else None
    block = _section_block(doc.lines[section.start], _body_lines(content), nested)
    new_lines = _splice(doc.lines, section.start, section.end, block)
    new_doc = parse(render(new_lines))
    return new_doc.text, new_doc.require(section.slug)


def append_to_section(text: str, heading: str, content: str) -> tuple[str, Section]:
    doc = parse(text)
    section = doc.require(heading)
    children = doc.children(section)
    own_end = children[0].start if children else section.end
    existing = _body_lines(render(doc.lines[section.body_start : own_end]))
    addition = _body_lines(content)
    nested = list(doc.lines[own_end : section.end]) if children else None
    block = _section_block(doc.lines[section.start], existing + addition, nested)
    new_lines = _splice(doc.lines, section.start, section.end, block)
    new_doc = parse(render(new_lines))
    return new_doc.text, new_doc.require(section.slug)


def create_section(
    text: str, heading: str, content: str, level: int = 2, parent: str | None = None
) -> tuple[str, Section]:
    if level not in (2, 3):
        raise ValueError("sections are level 2 or 3 headings")
    doc = parse(text)
    if doc.find(heading) is not None:
        raise SectionExists(heading)
    heading_line = f"{'#' * level} {heading.strip()}"
    block = _section_block(heading_line, _body_lines(content))
    if level == 3:
        if parent is None:
            raise ValueError("a level 3 section needs a parent heading")
        parent_section = doc.require(parent)
        insert_at = parent_section.end
    else:
        insert_at = len(doc.lines)
    lines = list(doc.lines)
    if insert_at and lines[insert_at - 1] != "":
        block.insert(0, "")
    new_lines = _splice(tuple(lines), insert_at, insert_at, block)
    new_doc = parse(render(new_lines))
    return new_doc.text, new_doc.require(slugify(heading))


def delete_section(text: str, heading: str) -> str:
    doc = parse(text)
    section = doc.require(heading)
    new_lines = _splice(doc.lines, section.start, section.end, [])
    return render(new_lines)


def apply_write(
    text: str,
    heading: str,
    content: str,
    mode: WriteMode,
    level: int = 2,
    parent: str | None = None,
) -> tuple[str, Section | None]:
    if mode is WriteMode.REPLACE:
        return replace_section(text, heading, content)
    if mode is WriteMode.APPEND:
        return append_to_section(text, heading, content)
    if mode is WriteMode.CREATE:
        return create_section(text, heading, content, level=level, parent=parent)
    return delete_section(text, heading), None


def replace_section_tree(text: str, heading: str, content: str) -> tuple[str, Section]:
    doc = parse(text)
    section = doc.require(heading)
    block = _section_block(doc.lines[section.start], _body_lines(content))
    new_lines = _splice(doc.lines, section.start, section.end, block)
    new_doc = parse(render(new_lines))
    return new_doc.text, new_doc.require(section.slug)
