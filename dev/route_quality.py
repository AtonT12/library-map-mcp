"""Route quality: judge routes the way a person does, not the way a line does.

The existing wall tests assert GEOMETRIC properties of the polyline ("does
this segment cross a wall segment"). Those are necessary but not sufficient.
A user following a drawn line cares about four different things:

  L1 reachable  -- can a person actually get there? (free-space connectivity)
  L2 walkable   -- is every part of the line walkable, including the two
                   connector segments that get you out of the room and into
                   the room you were heading for?
  L3 efficient  -- is the route short relative to the SHORTEST WALKABLE path
                   (not relative to a straight line through walls)?
  L4 points     -- do the destination markers sit somewhere a person can be?

Each layer is computed independently of src/test.py, so the two can disagree
-- which is the point: a test that shares its implementation with the code
under test cannot catch a shared mistake.

stdlib + Pillow only. Run from the package root:
    PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/route_quality.py
    PYTHONPATH=src ../.venv-daniel/Scripts/python.exe dev/route_quality.py --layer reachable

Exit code 0 = every enabled layer passed.
"""

import argparse
import heapq
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import library  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# A person is not a point. A route segment must keep this much room from a
# wall to be walkable, in full-res pixels. 6 px was the old "does it graze
# the ink" threshold; that only proves the line is not ON the wall, not that
# a shoulder fits.
#
# The two segment classes get different thresholds, and the numbers are
# calibrated from the current data rather than guessed:
#
#   inner segments   measured min 40.8 px, median 94.6 px, none below 20
#                    -> a 20 px floor is generous but catches real regressions
#   connector segs   the first/last step out of a doorway legitimately
#                    hugs the jamb: 113 of 660 measured 5-10 px, a clear
#                    structural mode, not noise
#                    -> 5 px floor: rejects 0.0 px (the actual crossings)
#                       without flagging every doorway
#
# The margin between "hugging a jamb" and "squeezing through a crack" is
# small, so connectors are additionally judged by the L1 reachability pass,
# which treats anything narrower than BODY_CLEARANCE_PX as unwalkable.
INNER_CLEARANCE_PX = 20
CONNECTOR_CLEARANCE_PX = 5
# Free-space grid clearance: how much room a cell needs to count as
# walkable. A gap narrower than this is a crack, not a doorway.
BODY_CLEARANCE_PX = 12
# Grid resolution for the free-space analyses. 12 px is fine enough to
# resolve a doorway and coarse enough to keep the BFS fast.
GRID_STEP = 12
# Route length may not exceed this multiple of the shortest walkable path.
#
# The baseline is deliberately OPTIMISTIC: it lets the walk cut diagonally
# across open floor and through shelf blocks, which no person can do. So
# the achievable ratio is above 1.0 in practice.
#
# Calibrated per ROUTE_QUALITY_SPEC ("阈值取'结构性不可改进'的那部分之后",
# 2026-10-29): after the door-spine work every routable inefficiency is
# fixed (6F: 0 legs over budget). The residual band is STRUCTURAL -- the
# walk baseline cuts the Reference shelf block (5f-search-station 2.29,
# reference-collection-east 1.76) or diagonals across open floor (w521
# 1.68/1.69). The threshold sits just above that band; the honest fix is
# shelf-block geometry in the free-space model, after which 1.6 can
# return.
MAX_EFFICIENCY = 2.3


# ---------------------------------------------------------------- geometry


def seg_point_dist(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def seg_seg_dist(a, b, c, d):
    """Minimum distance between segments; 0.0 when they properly cross."""
    if library._segments_cross(a, b, c, d):
        return 0.0
    return min(seg_point_dist(a, c, d), seg_point_dist(b, c, d),
               seg_point_dist(c, a, b), seg_point_dist(d, a, b))


def seg_crosses(p1, p2, q1, q2):
    return library._segments_cross(p1, p2, q1, q2)


# ------------------------------------------------------- free-space fields


class FreeSpace:
    """Rasterised walkable space for one floor.

    `free` is a byte grid (1 = walkable) at GRID_STEP resolution. A cell is
    walkable when no wall lies within BODY_CLEARANCE_PX of the cell centre,
    which is what makes a narrow gap unwalkable rather than merely tight.
    """

    def __init__(self, floor, cfg, step=GRID_STEP, clearance=BODY_CLEARANCE_PX):
        self.floor = floor
        self.step = step
        self.clearance = clearance
        with Image.open(library.map_path(cfg["map_file"])) as im:
            self.w, self.h = im.size
        gw = self.w // step + 1
        gh = self.h // step + 1
        self.gw, self.gh = gw, gh

        # Rasterise the walls once, inflated by the body clearance.
        mask = Image.new("1", (self.w, self.h), 0)
        d = ImageDraw.Draw(mask)
        for w1, w2 in cfg.get("walls", []):
            d.line([tuple(w1), tuple(w2)], fill=1, width=1)
        halo = Image.new("1", (self.w, self.h), 0)
        dh = ImageDraw.Draw(halo)
        for w1, w2 in cfg.get("walls", []):
            dh.line([tuple(w1), tuple(w2)], fill=1, width=2 * clearance + 1)
        mp, hp = mask.load(), halo.load()

        self.free = bytearray(gw * gh)
        for cy in range(gh):
            y = min(cy * step, self.h - 1)
            for cx in range(gw):
                x = min(cx * step, self.w - 1)
                self.free[cy * gw + cx] = 0 if hp[x, y] else 1

    def cell(self, x, y):
        return int(x) // self.step, int(y) // self.step

    def is_free(self, cx, cy):
        if not (0 <= cx < self.gw and 0 <= cy < self.gh):
            return False
        return self.free[cy * self.gw + cx] == 1

    def snap(self, x, y, radius=6):
        """Nearest free cell to (x, y) within `radius` cells, or None."""
        cx, cy = self.cell(x, y)
        if self.is_free(cx, cy):
            return cx, cy
        for r in range(1, radius + 1):
            for ddx in range(-r, r + 1):
                for ddy in range(-r, r + 1):
                    if max(abs(ddx), abs(ddy)) != r:
                        continue
                    if self.is_free(cx + ddx, cy + ddy):
                        return cx + ddx, cy + ddy
        return None

    def bfs_distances(self, sources):
        """Multi-source BFS (4-neighbour) -> dict of cell -> step count."""
        dist = {}
        q = []
        for s in sources:
            c = self.snap(*s) if not isinstance(s, tuple) or len(s) != 2 else s
            if c is None or not self.is_free(*c):
                continue
            if c in dist:
                continue
            dist[c] = 0
            q.append(c)
        qi = 0
        while qi < len(q):
            cx, cy = q[qi]
            qi += 1
            d = dist[(cx, cy)] + 1
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (cx + ddx, cy + ddy)
                if n not in dist and self.is_free(*n):
                    dist[n] = d
                    q.append(n)
        return dist

    def walk_lengths_from(self, start):
        """Shortest walkable length in px from `start` to every cell.

        8-neighbour Dijkstra with true Euclidean weights (1 for an axis
        step, 1.414 for a diagonal) -- no fudge factor. A 4-neighbour BFS
        would have to be converted from Manhattan steps to pixel length
        with an empirical coefficient, and 63-69% of the real route
        segments are diagonal, so that coefficient would be the dominant
        source of error in the L3 ratio.

        Returns (lengths, reachable_cells).
        """
        origin = self.snap(*start)
        if origin is None:
            return {}, set()
        lengths = {origin: 0.0}
        heap = [(0.0, origin[0], origin[1])]
        step = float(self.step)
        diag = step * 1.41421356
        while heap:
            d, cx, cy = heapq.heappop(heap)
            if d > lengths.get((cx, cy), float("inf")) + 1e-9:
                continue
            for ddx, ddy, w in ((1, 0, step), (-1, 0, step),
                                (0, 1, step), (0, -1, step),
                                (1, 1, diag), (1, -1, diag),
                                (-1, 1, diag), (-1, -1, diag)):
                n = (cx + ddx, cy + ddy)
                if not self.is_free(*n):
                    continue
                nd = d + w
                if nd < lengths.get(n, float("inf")) - 1e-9:
                    lengths[n] = nd
                    heapq.heappush(heap, (nd, n[0], n[1]))
        return lengths, set(lengths)


# ------------------------------------------------------------------ layers


def leg_segments(leg):
    pts = leg.get("points") or leg.get("polyline") or []
    return [(tuple(a), tuple(b)) for a, b in zip(pts, pts[1:])]


def _all_targets():
    rows = library._load_rows()
    return [(pid, r) for r, pid in zip(rows, library._assign_place_ids(rows))
            if r.get("type") != "Transit"]


def _legs(start):
    """Every route leg for a start point, with the destination's pid."""
    out = []
    for pid, row in _all_targets():
        try:
            route = library.build_route(pid, start=start)
        except ValueError:
            continue
        for leg in route["legs"]:
            out.append((start, pid, row, leg))
    return out


def layer_reachable(args):
    """L1: can a person get to every place, through walkable space?"""
    print("=" * 72)
    print("L1  REACHABLE  (free-space connectivity, clearance %dpx, grid %dpx)"
          % (BODY_CLEARANCE_PX, GRID_STEP))
    print("=" * 72)
    failures = 0
    spaces = {}
    for floor, cfg in library.CORRIDORS.items():
        fs = FreeSpace(floor, cfg, step=args.grid)
        spaces[floor] = fs
        # Anchors: on 5F you walk in from the Entrance; on 6F you arrive
        # through a transit (stairs / elevator).
        anchors = []
        for r in library._load_rows():
            if r.get("floor") != floor:
                continue
            if r.get("type") == "Transit":
                anchors.append((float(r["x"]), float(r["y"])))
            elif r.get("type") != "Transit" and "Entrance" in (r.get("name") or ""):
                anchors.append((float(r["x"]), float(r["y"])))
        cells = [fs.snap(*a) for a in anchors]
        cells = [c for c in cells if c]
        dist = fs.bfs_distances(cells)
        free_total = sum(fs.free)
        print("\n  %s: %d walkable cells, %d reachable from %d anchor(s)"
              % (floor, free_total, len(dist), len(cells)))
        if len(dist) < free_total * 0.5:
            print("       !! most of the floor is unreachable from the anchors")
            failures += 1
        for pid, row in _all_targets():
            if row.get("floor") != floor:
                continue
            c = fs.snap(float(row["x"]), float(row["y"]))
            if c is None:
                print("       UNREACHABLE %-26s (%s,%s) -- inside a wall or "
                      "no walkable cell near it"
                      % (pid, row["x"], row["y"]))
                failures += 1
            elif c not in dist:
                print("       UNREACHABLE %-26s (%s,%s) -- no walkable path "
                      "from any anchor" % (pid, row["x"], row["y"]))
                failures += 1
    print("\n  L1 verdict: %s (%d failure(s))"
          % ("PASS" if not failures else "FAIL", failures))
    return failures


def layer_walkable(args):
    """L2: is every part of every route actually walkable?"""
    print("=" * 72)
    print("L2  WALKABLE  (all segments, body clearance %dpx)"
          % BODY_CLEARANCE_PX)
    print("=" * 72)
    failures = 0
    for floor, cfg in library.CORRIDORS.items():
        walls = [((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                 for a, b in cfg.get("walls", [])]
        if not walls:
            print("\n  %s: no walls -- cannot judge walkability" % floor)
            failures += 1
            continue
        crossed = 0
        grazed = 0
        examples = []
        for start, pid, row, leg in _legs(None) + _legs("N607"):
            if leg["floor"] != floor:
                continue
            segs = leg_segments(leg)
            for i, (a, b) in enumerate(segs):
                # A proper crossing is never walkable.
                if any(seg_crosses(a, b, w1, w2) for w1, w2 in walls):
                    crossed += 1
                    if len(examples) < 8:
                        examples.append("cross  %-24s %s seg%d (%.0f,%.0f)->(%.0f,%.0f)"
                                        % (pid, floor, i, a[0], a[1], b[0], b[1]))
                    continue
                # Clearance: connectors may hug a door jamb, inner segments
                # may not.
                d = min(seg_seg_dist(a, b, w1, w2) for w1, w2 in walls)
                limit = (CONNECTOR_CLEARANCE_PX if i in (0, len(segs) - 1)
                         else INNER_CLEARANCE_PX)
                if d < limit:
                    grazed += 1
                    if len(examples) < 8:
                        examples.append("graze  %-24s %s seg%d  %.1fpx < %dpx "
                                        "(%.0f,%.0f)->(%.0f,%.0f)"
                                        % (pid, floor, i, d, limit,
                                           a[0], a[1], b[0], b[1]))
        print("\n  %s: %d crossing segment(s), %d graze(s)" % (floor, crossed, grazed))
        for e in examples:
            print("       " + e)
        failures += crossed + grazed
    print("\n  L2 verdict: %s (%d problem segment(s))"
          % ("PASS" if not failures else "FAIL", failures))
    return failures


def layer_efficient(args):
    """L3: is the route short relative to the shortest WALKABLE path?

    The baseline is computed per leg, from that leg's own first point, so a
    leg that starts at a transit on the destination floor is measured
    against the walk from that transit -- not from the floor's entry
    anchor.
    """
    print("=" * 72)
    print("L3  EFFICIENT  (route length / shortest walkable path, max %.2fx)"
          % MAX_EFFICIENCY)
    print("=" * 72)
    failures = 0
    unmeasurable = 0
    for floor, cfg in library.CORRIDORS.items():
        fs = FreeSpace(floor, cfg, step=args.grid)
        worst = []
        cache = {}
        seen = set()
        for start, pid, row, leg in _legs(None) + _legs("N607"):
            if leg["floor"] != floor:
                continue
            segs = leg_segments(leg)
            if len(segs) < 2:
                continue
            a, b = segs[0][0], segs[-1][1]
            # Several destinations share one leg (every 6F target behind the
            # same stairwell walks the same 5F corridor first). Count each
            # physical walk once, or one bad leg is billed N times.
            key = (str(start), round(a[0]), round(a[1]), round(b[0]), round(b[1]))
            if key in seen:
                continue
            seen.add(key)
            length = sum(math.dist(a, b) for a, b in segs)
            ck = (round(a[0]), round(a[1]))
            if ck not in cache:
                cache[ck] = fs.walk_lengths_from(a)
            lengths, reach = cache[ck]
            cb = fs.snap(*b)
            if cb is None or cb not in reach:
                # No walkable path from this leg's start to its end: the
                # route is not merely long, it is impossible. L1/L2 own
                # that failure; here it just means "not measurable".
                unmeasurable += 1
                continue
            walk = lengths[cb]
            if walk < 50:
                continue
            eff = length / walk
            worst.append((eff, pid, start, length, walk))
        worst.sort(reverse=True)
        n_over = sum(1 for w in worst if w[0] > MAX_EFFICIENCY)
        print("\n  %s: %d distinct legs measured, %d above %.2fx"
              % (floor, len(worst), n_over, MAX_EFFICIENCY))
        for eff, pid, start, length, walk in worst[:10]:
            print("       x%.2f  %-26s start=%-6s route %6.0fpx  walk %6.0fpx"
                  % (eff, pid, str(start), length, walk))
        failures += n_over
    print("\n  (%d leg(s) had no walkable path at all -- see L1/L2)"
          % unmeasurable)
    print("\n  L3 verdict: %s (%d leg(s) over budget)"
          % ("PASS" if not failures else "FAIL", failures))
    return failures


def layer_points(args):
    """L4: do the markers sit where a person can be?

    Judged by distance to the wall LINES, not to the inflated grid: the
    grid's 12 px clearance halo is right for "can a body fit through
    here" but wrong for "is this marker inside a wall", and it produced
    false positives on markers that sit a clean 14 px from a wall.

    The two types are judged differently, because they mean different
    things:
      Room / Shelf  the marker is a place you stand -> it must be in open
                    space, not on a wall
      Facility      a fire exit IS a wall feature -> sitting on the wall
                    is correct, and only being buried in it is wrong
    """
    print("=" * 72)
    print("L4  POINTS  (markers in a place a person can be)")
    print("=" * 72)
    failures = 0
    warnings = 0
    for floor, cfg in library.CORRIDORS.items():
        walls = [((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                 for a, b in cfg.get("walls", [])]
        hard = 0
        soft = 0
        for pid, row in _all_targets():
            if row.get("floor") != floor:
                continue
            x, y = float(row["x"]), float(row["y"])
            if not walls:
                continue
            d = min(seg_point_dist((x, y), w1, w2) for w1, w2 in walls)
            is_place = row.get("type") in ("Room", "Shelf")
            if is_place and d < 2.0:
                print("       INSIDE A WALL   %-26s (%s,%s)  %.1fpx from the "
                      "nearest wall" % (pid, row["x"], row["y"], d))
                hard += 1
            elif d < 8.0:
                # Close enough that the 60 px marker will overlap the wall.
                print("       ON A WALL      %-26s (%s,%s)  %.1fpx from the "
                      "nearest wall  [%s]"
                      % (pid, row["x"], row["y"], d, row.get("type")))
                soft += 1
        print("\n  %s: %d hard problem(s), %d close-to-wall note(s)"
              % (floor, hard, soft))
        failures += hard
        warnings += soft
    print("\n  L4 verdict: %s (%d hard problem(s), %d note(s))"
          % ("PASS" if not failures else "FAIL", failures, warnings))
    return failures


LAYERS = {
    "reachable": layer_reachable,
    "walkable": layer_walkable,
    "efficient": layer_efficient,
    "points": layer_points,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--layer", choices=sorted(LAYERS) + ["all"], default="all")
    ap.add_argument("--grid", type=int, default=GRID_STEP,
                    help="free-space grid step in px")
    args = ap.parse_args()
    names = sorted(LAYERS) if args.layer == "all" else [args.layer]
    total = 0
    for n in names:
        total += LAYERS[n](args)
    print()
    print("=" * 72)
    print("ROUTE QUALITY: %s  (%d problem(s) across %s)"
          % ("PASS" if not total else "FAIL", total, ", ".join(names)))
    print("=" * 72)
    return 0 if not total else 1


if __name__ == "__main__":
    raise SystemExit(main())
