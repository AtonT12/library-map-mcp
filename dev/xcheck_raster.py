"""Independent raster cross-check of the vector wall tests in src/test.py.

Method (deliberately different from library._segments_cross): rasterize each
floor's walls into full-resolution Pillow masks, then walk every route
polyline pixel by pixel. Crossing = a route pixel lands on a wall pixel;
tight = a route pixel lands inside the clearance halo but not on a wall.
The same sweep is also evaluated with the vector implementation; any
disagreement between the two methods is printed for adjudication and fails
the run, so a shared blind spot cannot stay silent.

Scope mirrors test.py::all_locations_wall_test (default 0/0 budgets):
same starts (None, "N607"), same Transit skip, same first/last-segment
exemption for clearance (but not for crossing).

stdlib + Pillow only. Run from the package root:
    PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/xcheck_raster.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import library  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# One owner (library.py): the halo the raster check draws is the clearance the
# router actually enforces, not a re-typed copy that can drift from it.
CLEARANCE = library.WALL_CLEARANCE_PX


def build_masks(floor):
    """Return (wall_px, halo_px) pixel-access objects for `floor`."""
    cfg = library.CORRIDORS[floor]
    with Image.open(library.map_path(cfg["map_file"])) as img:
        size = img.size
    wall = Image.new("1", size, 0)
    halo = Image.new("1", size, 0)
    dw, dh = ImageDraw.Draw(wall), ImageDraw.Draw(halo)
    for w1, w2 in cfg.get("walls", []):
        dw.line([tuple(w1), tuple(w2)], fill=1, width=1)
        dh.line([tuple(w1), tuple(w2)], fill=1, width=2 * CLEARANCE + 1)
    return wall.load(), halo.load(), size


def walk(a, b):
    """Yield integer pixels along segment a-b (inclusive, ~1px steps)."""
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    n = max(1, int(math.ceil(math.hypot(bx - ax, by - ay))))
    for i in range(n + 1):
        t = i / n
        yield int(round(ax + (bx - ax) * t)), int(round(ay + (by - ay) * t))


def leg_points(leg):
    for key in ("polyline", "points", "path", "pts"):
        if key in leg:
            return [tuple(p) for p in leg[key]]
    raise KeyError(f"leg has no point list (keys: {sorted(leg)})")


def raster_leg(wall_px, halo_px, size, pts, check_clearance_segs):
    """Return (crossed, tight) for one leg polyline via mask lookup."""
    w, h = size
    crossed = tight = False
    segs = list(zip(pts, pts[1:]))
    for idx, (a, b) in enumerate(segs):
        for x, y in walk(a, b):
            if not (0 <= x < w and 0 <= y < h):
                continue
            if wall_px[x, y]:
                crossed = True
            elif halo_px[x, y] and idx in check_clearance_segs:
                tight = True
    return crossed, tight


def vector_leg(floor, pts):
    """Return (crossed, tight) via the vector implementation under test."""
    walls = library.CORRIDORS[floor].get("walls", [])
    segs = list(zip(pts, pts[1:]))
    crossed = any(
        library._segments_cross(a, b, tuple(w[0]), tuple(w[1]))
        for a, b in segs for w in walls
    )
    inner = segs[1:-1]
    tight = (not crossed) and any(
        library._seg_wall_dist(floor, a, b) < CLEARANCE
        for a, b in inner
    )
    return crossed, tight


def main():
    rows = [r for r in library._load_rows() if r.get("type") != "Transit"]
    masks = {}
    for floor in library.CORRIDORS:
        masks[floor] = build_masks(floor)
        print(f"masks: {floor} size={masks[floor][2]} "
              f"walls={len(library.CORRIDORS[floor].get('walls', []))}")
    agg = {"r_cross": [], "r_tight": [], "v_cross": [], "v_tight": []}
    total = 0
    for start in (None, "N607"):
        for row in rows:
            try:
                route = library.build_route(row["name"], start=start)
            except ValueError:
                continue
            total += 1
            for leg in route["legs"]:
                floor = leg["floor"]
                pts = leg_points(leg)
                wall_px, halo_px, size = masks[floor]
                # Inner segments only, mirroring test.py's segs[1:-1]:
                # room connectors legitimately touch door gaps.
                inner_idx = set(range(1, len(pts) - 2))
                rc, rt = raster_leg(wall_px, halo_px, size, pts, inner_idx)
                vc, vt = vector_leg(floor, pts)
                key = (start, row["name"], floor)
                if rc:
                    agg["r_cross"].append(key)
                if rt:
                    agg["r_tight"].append(key)
                if vc:
                    agg["v_cross"].append(key)
                if vt:
                    agg["v_tight"].append(key)
    print(f"routes checked: {total}")
    print(f"raster: crossing={len(agg['r_cross'])} tight={len(agg['r_tight'])}")
    print(f"vector: crossing={len(agg['v_cross'])} tight={len(agg['v_tight'])}")
    problems = []
    for kind in ("cross", "tight"):
        r, v = set(agg[f"r_{kind}"]), set(agg[f"v_{kind}"])
        for key in sorted(r - v, key=repr):
            problems.append(f"raster-only {kind}: {key}")
        for key in sorted(v - r, key=repr):
            problems.append(f"vector-only {kind}: {key}")
    if problems:
        print("METHOD DISAGREEMENT (adjudicate by eye):")
        for p in problems[:20]:
            print("  " + p)
        raise SystemExit(1)
    assert not agg["r_cross"] and not agg["r_tight"], \
        f"raster found issues: {agg['r_cross'][:5]} {agg['r_tight'][:5]}"
    print("[ok] raster and vector agree: no crossings, no tight approaches")


if __name__ == "__main__":
    main()
