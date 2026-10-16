from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.text import Text

APP_NAME = "AI COMPUTER BRAIN"
POWERED_BY = "powered by Cognio Labs"
TAGLINE = "the shared memory that keeps every agent in sync"

GLYPHS: dict[str, tuple[str, ...]] = {
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "I": ("###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#.#.#", "#..##", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    " ": (".", ".", ".", ".", ".", ".", "."),
}
ROWS = 7
LETTER_GAP = 1
WORD_GAP = 1
LAYOUTS: tuple[tuple[str, ...], ...] = (
    ("AI COMPUTER BRAIN",),
    ("AI COMPUTER", "BRAIN"),
    ("AI", "COMPUTER", "BRAIN"),
)


@dataclass(frozen=True, slots=True)
class LogoColors:
    top: str
    middle: str
    bottom: str


def _hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _mix(color: str, target: tuple[int, int, int], amount: float) -> str:
    rgb = _hex_to_rgb(color)
    if rgb is None:
        return color
    mixed = tuple(round(c + (t - c) * amount) for c, t in zip(rgb, target, strict=True))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def lighten(color: str, amount: float) -> str:
    return _mix(color, (255, 255, 255), amount)


def darken(color: str, amount: float) -> str:
    return _mix(color, (0, 0, 0), amount)


def logo_colors(primary: str, accent: str) -> LogoColors:
    return LogoColors(lighten(primary, 0.45), primary, darken(accent, 0.1))


def band_for(row: int) -> int:
    if row < 2:
        return 0
    if row < 5:
        return 1
    return 2


def pixel_rows(text: str) -> list[str]:
    rows = [""] * ROWS
    for index, char in enumerate(text.upper()):
        glyph = GLYPHS.get(char)
        if glyph is None:
            continue
        gap = "." * (WORD_GAP if char == " " else LETTER_GAP) if index < len(text) - 1 else ""
        for row in range(ROWS):
            rows[row] += glyph[row] + gap
    return rows


def pixel_width(text: str) -> int:
    rows = pixel_rows(text)
    return len(rows[0]) if rows else 0


def choose_layout(width: int) -> tuple[tuple[str, ...], int]:
    for scale in (2, 1):
        for layout in LAYOUTS:
            if max(pixel_width(line) for line in layout) * scale + 2 <= width:
                return layout, scale
    return (), 0


def render_logo(primary: str, accent: str, width: int, color: bool = True, indent: int = 1) -> Text:
    layout, scale = choose_layout(width)
    text = Text(no_wrap=True)
    if not layout:
        text.append(" " * indent + APP_NAME, style=f"bold {primary}")
        return text
    colors = logo_colors(primary, accent)
    bands = (colors.top, colors.middle, colors.bottom)
    for line_index, line in enumerate(layout):
        if line_index:
            text.append("\n")
        for row, pixels in enumerate(pixel_rows(line)):
            style = f"on {bands[band_for(row)]}" if color else f"bold {bands[band_for(row)]}"
            text.append(" " * indent)
            cell = ("  " if color else "##")[:scale]
            for pixel in pixels:
                if pixel == "#":
                    text.append(cell, style=style)
                else:
                    text.append(" " * scale)
            if row < ROWS - 1 or line_index < len(layout) - 1:
                text.append("\n")
    return text


def logo_width(width: int) -> int:
    layout, scale = choose_layout(width)
    if not layout:
        return len(APP_NAME)
    return max(pixel_width(line) for line in layout) * scale


def render_signature(primary: str, muted: str, version: str, width: int, indent: int = 1) -> Text:
    span = logo_width(width)
    left = Text(no_wrap=True)
    left.append(" " * indent)
    left.append(f"v{version}", style=f"bold {primary}")
    left.append("  ")
    left.append(TAGLINE, style=muted)
    right = Text(POWERED_BY, style=f"bold {primary}")
    needed = len(left.plain) + 2 + len(right.plain)
    if needed > width:
        left.append("\n")
        left.append(" " * max(indent, indent + span - len(right.plain)))
        left.append_text(right)
        return left
    padding = max(2, span + indent - len(left.plain) - len(right.plain))
    left.append(" " * padding)
    left.append_text(right)
    return left


def print_logo(console: Console, primary: str, accent: str, muted: str, version: str) -> None:
    color = console.color_system is not None
    width = console.width
    console.print()
    console.print(render_logo(primary, accent, width, color=color))
    console.print(render_signature(primary, muted, version, width))
    console.print()
