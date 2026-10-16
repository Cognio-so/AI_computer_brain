from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "I": "pgup",
    "Q": "pgdn",
    "G": "home",
    "O": "end",
    "K": "left",
    "M": "right",
}
ANSI_KEYS = {
    "[A": "up",
    "[B": "down",
    "[5~": "pgup",
    "[6~": "pgdn",
    "[H": "home",
    "[F": "end",
    "[1~": "home",
    "[4~": "end",
    "[D": "left",
    "[C": "right",
    "OA": "up",
    "OB": "down",
    "OH": "home",
    "OF": "end",
}


@dataclass(frozen=True, slots=True)
class Choice:
    label: str
    value: Any = None
    hint: str = ""
    extra: Text | None = None


def keys_available(console: Console) -> bool:
    if os.environ.get("BRAIN_SIMPLE_MENUS"):
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    if console.legacy_windows or console.color_system is None:
        return False
    needed = ("msvcrt",) if os.name == "nt" else ("termios", "tty")
    return all(importlib.util.find_spec(name) is not None for name in needed)


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            return WINDOWS_KEYS.get(msvcrt.getwch(), "")
        return _translate(char)
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        char = sys.stdin.read(1)
        if char != "\x1b":
            return _translate(char)
        sequence = ""
        while select.select([sys.stdin], [], [], 0.05)[0]:
            sequence += sys.stdin.read(1)
            if sequence[-1].isalpha() or sequence.endswith("~"):
                break
        if not sequence:
            return "esc"
        return ANSI_KEYS.get(sequence, "")
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def _translate(char: str) -> str:
    if char in ("\r", "\n"):
        return "enter"
    if char == "\x1b":
        return "esc"
    if char in ("\x08", "\x7f"):
        return "backspace"
    if char == "\x03":
        raise KeyboardInterrupt
    if char == "\x04":
        raise EOFError
    return char if char.isprintable() else ""


def choose(
    console: Console,
    title: str,
    choices: Sequence[Choice],
    default: int = 0,
    ask: Callable[..., str] | None = None,
    filterable: bool | None = None,
    page: int = 10,
    cancel: str | None = None,
) -> Choice | None:
    if not choices:
        return None
    options = list(choices)
    if cancel:
        options.append(Choice(cancel, None))
    default = max(0, min(default, len(options) - 1))
    if not keys_available(console):
        return _numbered(console, title, options, default, ask, cancel)
    searchable = filterable if filterable is not None else len(options) > page
    query = ""
    index = default
    offset = 0
    while True:
        visible = _filtered(options, query)
        if not visible:
            visible = []
        index = max(0, min(index, max(len(visible) - 1, 0)))
        if index < offset:
            offset = index
        if index >= offset + page:
            offset = index - page + 1
        renderable = _render(title, visible, index, offset, page, query, searchable)
        with Live(renderable, console=console, transient=True, auto_refresh=False) as live:
            live.refresh()
            key = read_key()
        if key == "enter":
            if not visible:
                continue
            chosen = visible[index]
            if cancel and chosen.value is None and chosen.label == cancel:
                console.print(f"[muted]{title}: {cancel}[/muted]")
                return None
            console.print(f"[muted]{title}:[/muted] [accent]{chosen.label}[/accent]")
            return chosen
        if key == "esc":
            console.print(f"[muted]{title}: cancelled[/muted]")
            return None
        if key == "up":
            index = (index - 1) % len(visible) if visible else 0
        elif key == "down":
            index = (index + 1) % len(visible) if visible else 0
        elif key == "pgup":
            index = max(0, index - page)
        elif key == "pgdn":
            index = min(len(visible) - 1, index + page) if visible else 0
        elif key == "home":
            index = 0
        elif key == "end":
            index = len(visible) - 1 if visible else 0
        elif key == "backspace" and searchable:
            query = query[:-1]
            index = 0
        elif searchable and len(key) == 1:
            query += key
            index = 0
        elif key.isdigit() and not searchable:
            number = int(key)
            if 1 <= number <= len(visible):
                index = number - 1


def _filtered(options: list[Choice], query: str) -> list[Choice]:
    if not query:
        return options
    needle = query.casefold()
    return [c for c in options if needle in c.label.casefold() or needle in c.hint.casefold()]


def _render(
    title: str,
    visible: list[Choice],
    index: int,
    offset: int,
    page: int,
    query: str,
    searchable: bool,
) -> Group:
    lines: list[Text] = []
    header = Text(title, style="bold")
    header.append("   up/down move, Enter choose, Esc cancel", style="muted")
    if searchable:
        header.append(", type to filter", style="muted")
    lines.append(header)
    if searchable:
        lines.append(
            Text(
                f" filter: {query}" if query else " filter: (type to narrow the list)",
                style="muted",
            )
        )
    width = max((len(c.label) for c in visible), default=0)
    for position, choice in enumerate(visible[offset : offset + page], offset):
        selected = position == index
        line = Text(" > " if selected else "   ", style="accent" if selected else "muted")
        line.append(f"{choice.label:<{width}}", style="accent" if selected else "default")
        if choice.hint:
            line.append(f"  {choice.hint}", style="muted")
        if choice.extra is not None:
            line.append("  ")
            line.append_text(choice.extra)
        lines.append(line)
    if not visible:
        lines.append(Text("   nothing matches", style="muted"))
    hidden = len(visible) - (offset + page)
    if hidden > 0:
        lines.append(Text(f"   {hidden} more below", style="muted"))
    if offset > 0:
        lines.insert(2 if searchable else 1, Text(f"   {offset} more above", style="muted"))
    return Group(*lines)


def _numbered(
    console: Console,
    title: str,
    options: list[Choice],
    default: int,
    ask: Callable[..., str] | None,
    cancel: str | None,
) -> Choice | None:
    console.print()
    console.print(Text(title, style="bold"))
    width = max(len(c.label) for c in options)
    for number, choice in enumerate(options, 1):
        line = Text(f"  {number:>2}  ", style="bold")
        line.append(f"{choice.label:<{width}}", style="accent")
        if choice.hint:
            line.append(f"  {choice.hint}", style="muted")
        if choice.extra is not None:
            line.append("  ")
            line.append_text(choice.extra)
        console.print(line)
    asker = ask or _plain_prompt
    answer = str(asker("Choose", default=str(default + 1))).strip()
    if not answer:
        chosen = options[default]
    elif answer.isdigit() and 1 <= int(answer) <= len(options):
        chosen = options[int(answer) - 1]
    else:
        needle = answer.casefold()
        chosen = next(
            (
                c
                for c in options
                if c.label.casefold() == needle or str(c.value).casefold() == needle
            ),
            None,
        ) or next((c for c in options if needle in c.label.casefold()), None)
        if chosen is None:
            console.print(f"[warning]no option matches {answer!r}[/warning]")
            return None
    if cancel and chosen.label == cancel and chosen.value is None:
        return None
    return chosen


def _plain_prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{text}{suffix}: ").strip()
    return answer or default
