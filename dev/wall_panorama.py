"""Per-floor panorama overviews with every wall overlaid and numbered.

Complements dev/wall_tiles.py (detail tiles): one downsampled full-floor
image per floor so the reviewer can navigate by wall index. Walls are
colour-coded from review_out/verdicts.json ({floor: {idx: P/V/N}}):
P = green, V = red with a cross, N = yellow, unjudged = red. Re-run after
every new verdict batch; tile numbering matches wall_tiles.py.

Also patches review_out/index.html (panorama block + cross-links to the
3D mockup index) idempotently via stable markers.

stdlib + Pillow only. Run from the package root:
    PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/wall_panorama.py
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import library  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "review_out")
VERDICTS = os.path.join(OUT, "verdicts.json")
MAX_W = 2000  # panorama long side

COLORS = {"P": "green", "V": "red", "N": "yellow", None: "red"}


def load_verdicts():
    if os.path.exists(VERDICTS):
        with open(VERDICTS, encoding="utf-8") as f:
            return json.load(f)
    return {}


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def panorama(floor, cfg, base, verdicts):
    scale = min(1.0, MAX_W / base.width)
    small = base.resize((int(base.width * scale), int(base.height * scale)))
    d = ImageDraw.Draw(small)
    lw = max(2, int(5 * scale))
    fnt = font(20)
    for i, (w1, w2) in enumerate(cfg.get("walls", [])):
        v = verdicts.get(floor, {}).get(str(i))
        a = (w1[0] * scale, w1[1] * scale)
        b = (w2[0] * scale, w2[1] * scale)
        d.line([a, b], fill=COLORS.get(v, "red"), width=lw)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        label = str(i)
        box = [mx - 2, my - 34, mx + 10 * len(label) + 8, my - 8]
        d.rectangle(box, fill=COLORS.get(v, "red"))
        d.text((mx + 3, my - 32), label, fill="white", font=fnt)
        if v == "V":
            r = 16
            d.line([(mx - r, my - r), (mx + r, my + r)],
                   fill="white", width=3)
            d.line([(mx - r, my + r), (mx + r, my - r)],
                   fill="white", width=3)
    d.rectangle([2, 2, 420, 44], fill="black")
    counts = {k: sum(1 for w in range(len(cfg.get("walls", [])))
                     if verdicts.get(floor, {}).get(str(w)) == k)
              for k in ("P", "V", "N")}
    todo = len(cfg.get("walls", [])) - sum(counts.values())
    d.text((10, 10),
           f"{floor}: {len(cfg.get('walls', []))} walls "
           f"(P{counts['P']}/V{counts['V']}/N{counts['N']}/todo{todo})",
           fill="white", font=font(22))
    path = os.path.join(OUT, floor, "panorama.png")
    small.save(path)
    return path


def patch_index():
    idx = os.path.join(OUT, "index.html")
    if not os.path.exists(idx):
        return
    with open(idx, encoding="utf-8") as f:
        html = f.read()
    nav = ("<p><a href='mock_3d_index.html'>3D mockups v1/v2/v3</a> · "
           "verdicts go in chat as e.g. <b>V: 6F #12, N: 5F #2</b> "
           "(unlisted walls default to P)</p>")
    if "mock_3d_index.html" not in html:
        html = html.replace("</h1>", "</h1>\n" + nav, 1)
    block = ("<h2>Panoramas (all walls, numbered)</h2>"
             "<p><img src='5F/panorama.png'></p>"
             "<p><img src='6F/panorama.png'></p>")
    if "Panoramas (all walls" not in html:
        html = html.replace("<table>", block + "\n<table>", 1)
    with open(idx, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(VERDICTS):
        with open(VERDICTS, "w", encoding="utf-8") as f:
            json.dump({"5F": {"8": "V"}, "6F": {}}, f, indent=1)
        print("seeded", VERDICTS, "with 5F #8 = V")
    verdicts = load_verdicts()
    for floor, cfg in library.CORRIDORS.items():
        with Image.open(library.map_path(cfg["map_file"])) as img:
            base = img.convert("RGB")
            panorama(floor, cfg, base, verdicts)
    patch_index()
    print("[ok] panoramas + index links in", OUT)


if __name__ == "__main__":
    main()
