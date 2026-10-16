"""Static 3D mockups (v1/v2/v3) for visual review before touching viewer.js.

Reimplements the viewer.js cabinet-oblique projection offline in pure
Python + inline SVG (zero dependencies beyond stdlib+Pillow):
    project(x, y, z) = [S*(x + SH*y), S*SQ*y - z]
v1 = polished current math (floating route ribbon, soft shadow, slab
     gradients, floor plates, fat transit connector).
v2 = v1 at three azimuths side by side (basis rotation before projection).
v3 = v1 + walls extruded as translucent vertical quads (geometry is real
     corridors.json data; productising it needs wall plumbing into the
     view, which is a separate decision).
Uses the LIVE get_route("N607") plan so the mock reflects current data.
Run: PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/mock_3d.py
Output: review_out/mock_3d_{v1,v2,v3}.svg (+ route_live.json).
"""

import base64
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import library  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "review_out")
SQ = 0.62
SH = (1 - SQ) * 0.18


def load_plan():
    plan = library.get_route("N607")
    d = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    with open(os.path.join(OUT, "route_live.json"), "w",
              encoding="utf-8") as f:
        json.dump(d, f)
    return d


def webp_href(map_file):
    small = ("5f_small.webp" if map_file.startswith("5f") else
             "6f_small.webp")
    with open(os.path.join(ROOT, "src", "static", small), "rb") as f:
        return "data:image/webp;base64," + base64.b64encode(f.read()).decode()


def fit(legs, vw, vh, pad=30):
    uw = max(l["map_size"][0] for l in legs)
    uh = max(l["map_size"][1] for l in legs)
    s = min((vw - 2 * pad) / (uw * (1 + SH) + 1),
            (vh * 0.92 - 2 * pad) / (uh * SQ + 0.06 * uh + 1))
    return {"S": s, "SQ": SQ, "SH": SH, "UW": uw, "UH": uh,
            "z6": 0.06 * uh * s, "thick": 0.012 * uh * s,
            "lift": 0.02 * uh * s}


def rot(p, th):
    c, s = math.cos(th), math.sin(th)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def project(x, y, z, P, th=None):
    th = P.get("TH", 0.0) if th is None else th
    if th:
        x, y = rot((x, y), th)
    return (P["S"] * (x + P["SH"] * y), P["S"] * P["SQ"] * y - z)


def img_matrix(P, ox, oy, z):
    """Affine image matrix composing azimuth rotation + cabinet tilt."""
    th = P.get("TH", 0.0)
    c, s, S = math.cos(th), math.sin(th), P["S"]
    a = S * (c + P["SH"] * s)
    b = S * P["SQ"] * s
    cc = S * (-s + P["SH"] * c)
    d = S * P["SQ"] * c
    return (a, b, cc, d, ox, oy - z)


def pts_str(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt")


class SVG:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.b = [
            f"<svg xmlns='http://www.w3.org/2000/svg' "
            f"xmlns:xlink='http://www.w3.org/1999/xlink' "
            f"width='{w}' height='{h}' viewBox='0 0 {w} {h}'>",
            "<defs><linearGradient id='slab' x1='0' y1='0' x2='0' y2='1'>"
            "<stop offset='0' stop-color='#f4f6f8'/>"
            "<stop offset='1' stop-color='#d7dde3'/>"
            "</linearGradient>"
            "<filter id='soft' x='-40%' y='-40%' width='180%' height='180%'>"
            "<feGaussianBlur stdDeviation='10'/></filter></defs>",
            f"<rect width='{w}' height='{h}' fill='#20242a'/>"]

    def add(self, s):
        self.b.append(s)

    def polygon(self, pts, **kw):
        a = " ".join(f"{k.replace('_','-')}='{v}'" for k, v in kw.items())
        self.b.append(f"<polygon points='{pts_str(pts)}' {a}/>")

    def polyline(self, pts, **kw):
        a = " ".join(f"{k.replace('_','-')}='{v}'" for k, v in kw.items())
        self.b.append(f"<polyline points='{pts_str(pts)}' {a}/>")

    def text(self, x, y, s, size=26, fill="#fff", ox=0, oy=0, anchor="start"):
        self.b.append(
            f"<text x='{x+ox:.1f}' y='{y+oy:.1f}' font-size='{size}' "
            f"font-family='Arial' font-weight='bold' fill='{fill}' "
            f"text-anchor='{anchor}'>{esc(s)}</text>")

    def save(self, path):
        self.b.append("</svg>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.b))
        print("wrote", path)


def draw_floor(g, leg, hrefs, P, current, ox, oy, walls=None):
    fw, fh = leg["floor"] and leg["map_size"]
    z = P["z6"] if leg["floor"] == "6F" else 0.0
    op = 1.0 if current else 0.45
    g.add(f"<g opacity='{op}'>")
    c = [project(x, y, z, P) for x, y in
         [(0, 0), (fw, 0), (fw, fh), (0, fh)]]
    c = [(x + ox, y + oy) for x, y in c]
    sh = [(x + 12, y + 14) for x, y in c]
    g.polygon(sh, fill="#000", opacity=0.35, filter="url(#soft)")
    t = P["thick"]
    for q in ([c[0], c[1], [c[1][0], c[1][1] + t], [c[0][0], c[0][1] + t]],
              [c[1], c[2], [c[2][0], c[2][1] + t], [c[1][0], c[1][1] + t]]):
        g.polygon(q, fill="#8f99a3", opacity=0.9)
    g.polygon(c, fill="url(#slab)", stroke="#ffffff", stroke_width=2)
    a, b, cc, d, e, f = img_matrix(P, ox, oy, z)
    g.add(
        f"<image xlink:href='{hrefs[leg['floor']]}' x='0' y='0' "
        f"width='{fw}' height='{fh}' opacity='0.92' transform='matrix("
        f"{a:.5f} {b:.5f} {cc:.5f} {d:.5f} {e:.1f} {f:.1f})'/>")
    if walls:
        wh = 0.035 * P["UH"] * P["S"]
        for w1, w2 in walls.get(leg["floor"], []):
            a = project(w1[0], w1[1], z, P)
            b = project(w2[0], w2[1], z, P)
            a = (a[0] + ox, a[1] + oy)
            b = (b[0] + ox, b[1] + oy)
            g.polygon([a, b, (b[0], b[1] - wh), (a[0], a[1] - wh)],
                      fill="#c0392b", opacity=0.28)
            g.polyline([ (a[0], a[1] - wh), (b[0], b[1] - wh)],
                       fill="none", stroke="#c0392b", stroke_width=2)
    poly = leg["polyline"]
    if len(poly) >= 2:
        flat = [(project(p[0], p[1], z, P)[0] + ox,
                 project(p[0], p[1], z, P)[1] + oy) for p in poly]
        g.polyline([(x + 8, y + 10) for x, y in flat], fill="none",
                   stroke="#000", stroke_width=9, opacity=0.30,
                   filter="url(#soft)", stroke_linejoin="round")
        up = [(project(p[0], p[1], z + P["lift"], P)[0] + ox,
               project(p[0], p[1], z + P["lift"], P)[1] + oy) for p in poly]
        g.polyline(up, fill="none", stroke="white", stroke_width=10,
                   stroke_linejoin="round")
        g.polyline(up, fill="none", stroke="#1E6FFF", stroke_width=5.5,
                   stroke_linejoin="round")
        for p, col in ((poly[0], "#22a355"), (poly[-1], "#d33")):
            q = project(p[0], p[1], z + P["lift"], P)
            g.add(
                f"<circle cx='{q[0]+ox:.1f}' cy='{q[1]+oy:.1f}' r='11' "
                f"fill='{col}' stroke='white' stroke-width='3.5'/>")
    lx, ly = c[0]
    g.add(f"<rect x='{lx-8:.1f}' y='{ly-46:.1f}' width='64' height='36' "
          f"rx='7' fill='#111' opacity='0.85'/>")
    g.text(lx + 24, ly - 19, leg["floor"], size=24, anchor="middle")
    g.add("</g>")


def draw_link(g, legs, P, ox, oy):
    if len(legs) < 2:
        return
    t = legs[0].get("transit") or {}
    def tp(leg, use_end):
        st = [s for s in leg.get("steps", [])
              if s.get("kind") == "transit" and s.get("point")]
        if st:
            return st[0]["point"]
        p = leg["polyline"]
        return p[-1] if use_end else p[0]
    down = t.get("direction") == "down"
    pA, pB = tp(legs[0], not down), tp(legs[1], down)
    zA = P["z6"] if legs[0]["floor"] == "6F" else 0.0
    zB = P["z6"] if legs[1]["floor"] == "6F" else 0.0
    qA = project(pA[0], pA[1], zA, P)
    qB = project(pB[0], pB[1], zB, P)
    qA, qB = (qA[0] + ox, qA[1] + oy), (qB[0] + ox, qB[1] + oy)
    g.add(f"<line x1='{qA[0]:.1f}' y1='{qA[1]:.1f}' x2='{qB[0]:.1f}' "
          f"y2='{qB[1]:.1f}' stroke='#c00' stroke-width='7' "
          f"stroke-linecap='round'/>")
    for q in (qA, qB):
        g.add(f"<circle cx='{q[0]:.1f}' cy='{q[1]:.1f}' r='9' fill='#c00' "
              f"stroke='white' stroke-width='3'/>")
    name = t.get("name", "transit")
    arrow = "↓" if down else "↑"
    g.text((qA[0] + qB[0]) / 2 + 12, (qA[1] + qB[1]) / 2,
           f"{arrow} {name}", size=26, fill="#ff6b6b")


def panel(g, legs, hrefs, P, ox, oy, title, cur_floor, walls=None):
    cur = next((l for l in legs if l["floor"] == cur_floor), legs[0])
    others = [l for l in legs if l is not cur]
    order = others + [cur] if cur_floor == "6F" else \
        list(reversed(others)) + [cur]
    for l in order:
        draw_floor(g, l, hrefs, P, l is cur, ox, oy, walls)
    draw_link(g, legs, P, ox, oy)
    g.text(ox + 14, oy + 34, title, size=24, fill="#ffd166")


def main():
    os.makedirs(OUT, exist_ok=True)
    plan = load_plan()
    legs = plan["legs"]
    hrefs = {fl: webp_href(next(
        l["map_file"] for l in legs if l["floor"] == fl)) for fl in
        {l["floor"] for l in legs}}
    cur = "6F"

    g = SVG(1400, 900)
    P = fit(legs, 1400, 900)
    panel(g, legs, hrefs, P, 40, 90, "v1 · polished ribbon + plates", cur)
    g.save(os.path.join(OUT, "mock_3d_v1.svg"))

    g = SVG(2700, 760)
    for k, th in enumerate((math.radians(-18), 0.0, math.radians(22))):
        P = fit(legs, 860, 680)
        P["TH"] = th
        panel(g, legs, hrefs, P, 20 + k * 890, 60,
              f"v2 · azimuth {math.degrees(th):.0f}°", cur)
    g.save(os.path.join(OUT, "mock_3d_v2.svg"))

    walls = {fl: [ (tuple(w[0]), tuple(w[1])) for w in
                   library.CORRIDORS[fl].get("walls", [])]
             for fl in library.CORRIDORS}
    g = SVG(1400, 900)
    P = fit(legs, 1400, 900)
    panel(g, legs, hrefs, P, 40, 90, "v3 · v1 + extruded walls", cur,
          walls)
    g.save(os.path.join(OUT, "mock_3d_v3.svg"))

    with open(os.path.join(OUT, "mock_3d_index.html"), "w",
              encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>3D mockups</title>"
                "<style>body{background:#111;color:#eee;font-family:sans-serif}"
                "img{max-width:100%;border:1px solid #555}</style></head><body>"
                "<h1>3D mockups (live route data)</h1>"
                "<h2>v1 polished</h2><img src='mock_3d_v1.svg'>"
                "<h2>v2 azimuths</h2><img src='mock_3d_v2.svg'>"
                "<h2>v3 extruded walls</h2><img src='mock_3d_v3.svg'>"
                "</body></html>")
    print("[ok] mockups in", OUT)


if __name__ == "__main__":
    main()
