#!/usr/bin/env python3
"""Render a blog chart's SVG to the PNG that social previews need.

LinkedIn, Slack and most feed readers ignore an SVG og:image and show the
card with no picture at all. Every chart here is authored as SVG — it stays
sharp and is a text file in review — so each one also needs a raster twin.

Output is always 2400x1120: the size the first PNGs here used, and twice
1200x560 so the common case is a clean 2x. Charts are not all that shape,
so the image is scaled to fit and centred on its own surface colour. Fitting
rather than cropping is the point — a cropped chart loses a label or an axis,
and nobody sees it happen because the card still looks fine.

Usage:
    scripts/svg_to_png.py web/static/blog/*.svg        # only missing ones
    scripts/svg_to_png.py --force web/static/blog/a.svg

Needs cairosvg and Pillow (not in requirements.txt — this runs locally when
writing a post, never in the container).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WIDTH, HEIGHT = 2400, 1120
# The surface every chart draws itself on; used for the letterbox so the pad
# is invisible rather than a white border around an off-white chart.
DEFAULT_SURFACE = "#fcfcfb"


def surface_of(svg: str) -> str:
    """The chart's own background, from its first full-size <rect fill=…>."""
    m = re.search(r'<rect[^>]*\bwidth="(\d+)"[^>]*\bfill="(#[0-9a-fA-F]{3,8})"', svg)
    return m.group(2) if m else DEFAULT_SURFACE


def viewbox(svg: str) -> tuple[float, float]:
    m = re.search(r'viewBox="\s*0\s+0\s+([\d.]+)\s+([\d.]+)', svg)
    if not m:
        raise ValueError("no viewBox")
    return float(m.group(1)), float(m.group(2))


def render(src: Path, dst: Path) -> str:
    import cairosvg
    from PIL import Image

    svg = src.read_text(encoding="utf-8")
    vw, vh = viewbox(svg)
    scale = min(WIDTH / vw, HEIGHT / vh)
    w, h = round(vw * scale), round(vh * scale)

    tmp = dst.with_suffix(".tmp.png")
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(tmp),
                     output_width=w, output_height=h)
    chart = Image.open(tmp).convert("RGBA")
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), surface_of(svg))
    canvas.alpha_composite(chart, ((WIDTH - w) // 2, (HEIGHT - h) // 2))
    canvas.convert("RGB").save(dst, "PNG", optimize=True)
    tmp.unlink()
    return f"{vw:.0f}x{vh:.0f} → {w}x{h} centred on {WIDTH}x{HEIGHT}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("svgs", nargs="+", type=Path)
    ap.add_argument("--force", action="store_true", help="rewrite PNGs that already exist")
    args = ap.parse_args()

    failed = 0
    for src in args.svgs:
        dst = src.with_suffix(".png")
        if dst.exists() and not args.force:
            print(f"  skip   {src.name} — {dst.name} exists")
            continue
        try:
            print(f"  ok     {src.name}: {render(src, dst)}")
        except Exception as exc:
            print(f"  FAIL   {src.name}: {type(exc).__name__}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
