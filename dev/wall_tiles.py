"""Per-wall review gallery for full manual verification of corridors.json.

For every wall segment on every floor, renders a cropped tile of the base
map with the data wall overlaid in red, plus endpoint zooms for door-gap
judgement. Emits review_out/<floor>/tile_<i>.png and review_out/index.html.
The reviewer marks each wall P (placed), V (void/phantom, delete) or
N (nudge, needs editor tuning); verdicts are collected in chat and land in
docs/wall_verify_*.md, then applied to corridors.json.

stdlib + Pillow only. Run from the package root:
    PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/wall_tiles.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import library  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "review_out")
PAD = 220          # context around the wall bbox in the main tile
ZOOM = 320         # endpoint zoom window (square, full-res px)
LABEL_H = 64       # caption strip height


def text(draw, xy, s):
    draw.rectangle([xy, (xy[0] + 8 * len(s) + 12, xy[1] + 26)], fill="black")
    draw.text((xy[0] + 6, xy[1] + 4), s, fill="white")


def wall_tile(base, w1, w2, idx, floor):
    """Main tile: padded crop with the data wall in red + midpoint tick."""
    x0 = max(0, int(min(w1[0], w2[0]) - PAD))
    y0 = max(0, int(min(w1[1], w2[1]) - PAD))
    x1 = min(base.width, int(max(w1[0], w2[0]) + PAD))
    y1 = min(base.height, int(max(w1[1], w2[1]) + PAD))
    crop = base.crop((x0, y0, x1, y1)).copy()
    d = ImageDraw.Draw(crop)
    a = (w1[0] - x0, w1[1] - y0)
    b = (w2[0] - x0, w2[1] - y0)
    d.line([a, b], fill="red", width=5)
    for p in (a, b):
        d.ellipse([p[0] - 9, p[1] - 9, p[0] + 9, p[1] + 9],
                  outline="yellow", width=3)
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    dx, dy = b[0] - a[0], b[1] - a[1]
    leng = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / leng * 28, dx / leng * 28
    d.line([(mx - nx, my - ny), (mx + nx, my + ny)], fill="white", width=3)
    length = math.hypot(w2[0] - w1[0], w2[1] - w1[1])
    cap = Image.new("RGB", (crop.width, LABEL_H), "black")
    dc = ImageDraw.Draw(cap)
    text(dc, (6, 6), f"{floor} wall #{idx} len={length:.0f}px")
    text(dc, (6, 34),
         f"({w1[0]:.0f},{w1[1]:.0f}) -> ({w2[0]:.0f},{w2[1]:.0f})")
    out = Image.new("RGB", (crop.width, crop.height + LABEL_H), "black")
    out.paste(crop, (0, 0))
    out.paste(cap, (0, crop.height))
    return out


def endpoint_zoom(base, p, tag):
    """Square zoom on a wall endpoint for door-gap judgement."""
    x0 = max(0, int(p[0] - ZOOM / 2))
    y0 = max(0, int(p[1] - ZOOM / 2))
    crop = base.crop((x0, y0, x0 + ZOOM, y0 + ZOOM)).copy()
    d = ImageDraw.Draw(crop)
    c = (p[0] - x0, p[1] - y0)
    d.ellipse([c[0] - 11, c[1] - 11, c[0] + 11, c[1] + 11],
              outline="yellow", width=3)
    d.line([(c[0] - 20, c[1]), (c[0] + 20, c[1])], fill="yellow", width=2)
    d.line([(c[0], c[1] - 20), (c[0], c[1] + 20)], fill="yellow", width=2)
    cap = Image.new("RGB", (ZOOM, 30), "black")
    ImageDraw.Draw(cap).text((6, 6), tag, fill="white")
    out = Image.new("RGB", (ZOOM, ZOOM + 30), "black")
    out.paste(crop, (0, 0))
    out.paste(cap, (0, ZOOM))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for floor, cfg in library.CORRIDORS.items():
        fdir = os.path.join(OUT, floor)
        os.makedirs(fdir, exist_ok=True)
        with Image.open(library.map_path(cfg["map_file"])) as img:
            base = img.convert("RGB")
            for i, (w1, w2) in enumerate(cfg.get("walls", [])):
                w1 = [float(w1[0]), float(w1[1])]
                w2 = [float(w2[0]), float(w2[1])]
                wall_tile(base, w1, w2, i, floor).save(
                    os.path.join(fdir, f"tile_{i:02d}.png"))
                endpoint_zoom(base, w1, f"{floor} #{i} end-A").save(
                    os.path.join(fdir, f"tile_{i:02d}_A.png"))
                endpoint_zoom(base, w2, f"{floor} #{i} end-B").save(
                    os.path.join(fdir, f"tile_{i:02d}_B.png"))
                leng = math.hypot(w2[0] - w1[0], w2[1] - w1[1])
                rows.append((floor, i, leng, w1, w2))
    parts = ["<html><head><meta charset='utf-8'><title>Wall review</title>",
             "<style>body{font-family:sans-serif;background:#111;color:#eee}",
             "img{max-width:900px;border:1px solid #555}td{padding:8px;vertical-align:top}</style></head><body>",
             "<h1>Wall review — mark each P / V / N</h1>",
             "<p>P = placed on a real wall. V = void/phantom, delete. "
             "N = nudged, needs editor tuning (note direction).</p><table>"]
    for floor, i, leng, w1, w2 in rows:
        parts.append(
            f"<tr><td><h2>{floor} wall #{i}</h2>"
            f"<p>len={leng:.0f}px<br>({w1[0]:.0f},{w1[1]:.0f}) -&gt; "
            f"({w2[0]:.0f},{w2[1]:.0f})<br>verdict: ______</p>"
            f"<img src='{floor}/tile_{i:02d}.png'><br>"
            f"<img src='{floor}/tile_{i:02d}_A.png'> "
            f"<img src='{floor}/tile_{i:02d}_B.png'></td></tr>")
    parts.append("</table></body></html>")
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"[ok] {len(rows)} walls -> {os.path.abspath(OUT)}/index.html")


if __name__ == "__main__":
    main()
