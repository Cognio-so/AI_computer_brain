from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from second_brain.banner import APP_NAME, POWERED_BY, TAGLINE, band_for, logo_colors, pixel_rows

PIXEL = 11
GAP = 1
PADDING = 56
BACKGROUND = "#0b1220"
MUTED = "#8b9bb4"


def build(primary: str, accent: str, out: Path) -> None:
    colors = logo_colors(primary, accent)
    bands = (colors.top, colors.middle, colors.bottom)
    rows = pixel_rows(APP_NAME)
    width = len(rows[0]) * PIXEL + 2 * PADDING
    logo_height = len(rows) * PIXEL
    height = PADDING + logo_height + 92
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{APP_NAME}">',
        f'<rect width="{width}" height="{height}" rx="18" fill="{BACKGROUND}"/>',
    ]
    for row_index, row in enumerate(rows):
        fill = bands[band_for(row_index)]
        y = PADDING + row_index * PIXEL
        for col, pixel in enumerate(row):
            if pixel == "#":
                x = PADDING + col * PIXEL
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{PIXEL - GAP}" height="{PIXEL - GAP}" '
                    f'rx="1.5" fill="{fill}"/>'
                )
    text_y = PADDING + logo_height + 44
    parts.append(
        f'<text x="{PADDING}" y="{text_y}" font-family="ui-monospace, SFMono-Regular, Menlo, '
        f'Consolas, monospace" font-size="22" fill="{MUTED}">{TAGLINE}</text>'
    )
    parts.append(
        f'<text x="{width - PADDING}" y="{text_y}" text-anchor="end" font-family="ui-monospace, '
        f'SFMono-Regular, Menlo, Consolas, monospace" font-size="18" fill="{primary}">'
        f"{POWERED_BY}</text>"
    )
    parts.append("</svg>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    primary = sys.argv[1] if len(sys.argv) > 1 else "#38bdf8"
    accent = sys.argv[2] if len(sys.argv) > 2 else "#a78bfa"
    target = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("docs/assets/banner.svg")
    build(primary, accent, target)
    print(target)
