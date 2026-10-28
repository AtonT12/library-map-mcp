import csv
import difflib
import hashlib
import heapq
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from PIL import Image, ImageDraw

import models

# Config: absolute path based on project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, 'locations.csv')
CORRIDORS_FILE = os.path.join(BASE_DIR, 'corridors.json')
# Data-side point relocation: keyed by place_id, applied once in _load_rows()
# so all coordinate consumers share the same effective coords. locations.csv
# itself is never modified (it is upstream data).
OVERRIDES_FILE = os.path.join(BASE_DIR, 'src', 'route_overrides.json')
# Floor map images live under maps/. Stored references (locations.csv column
# and corridors.json "map_file") keep bare filenames; resolve them here.
MAPS_DIR = os.path.join(BASE_DIR, 'maps')


def map_path(map_file):
    """Absolute path for a floor map image name (bare or subfoldered)."""
    return os.path.join(MAPS_DIR, os.path.basename(map_file))

# Kill-switch mirror (plan Step 6/L1): when "0", the image tools keep today's
# exact JPEG bytes and no ui:// metadata is advertised. mcp_server.py reads
# the same variable; the environment is the single source of truth.
ENABLE_MCP_APPS = os.environ.get("ENABLE_MCP_APPS", "1") == "1"

# A query "looks like a call number" when it is a Library of Congress style
# class (1-3 letters) optionally followed by a number, e.g. QA76, PN, B105.3.
CALL_NUMBER_RE = re.compile(r'^[A-Z]{1,3}\d')

# Everyday words patrons use for facilities, rewritten to the wording used in
# locations.csv before matching (e.g. "WC" -> "RESTROOM").
SYNONYMS = [
    (re.compile(r'\b(WCS?|TOILETS?|BATHROOMS?|WASHROOMS?|LAVATOR(?:Y|IES)|RESTROOMS)\b'), 'RESTROOM'),
    (re.compile(r'\b(WATER FOUNTAINS?|DRINKING WATER|FOUNTAINS?|WATER(?! DISPENSER))\b'), 'WATER DISPENSER'),
    (re.compile(r'\b(PRINTERS|PRINTING|PRINT|COPIERS?|COPY|SCANNERS?|SCAN)\b'), 'PRINTER'),
    (re.compile(r'\bBOOK RETURNS?\b'), 'BOOK DROP'),
    (re.compile(r'\bEAR ?PLUGS?\b'), 'EARPLUGS'),
    (re.compile(r'\bEMERGENCY EXITS?\b'), 'FIRE EXIT'),
    (re.compile(r'\b(PHONE BOOTHS|CALL BOOTHS?)\b'), 'PHONE BOOTH'),
]

# Matches a leading floor tag in a facility name, e.g. "5F Restroom".
FLOOR_PREFIX_RE = re.compile(r'^[56]F\s+')

# Corridor "spine" per floor: a graph of hallway waypoints (full-res pixel
# coords) connected by segments, plus optional wall segments. Directions route
# along the spine and branch off it only where the connector does not cross a
# drawn wall, so the path stays in corridors and enters rooms through doorways.
#
# The spine and walls are authored visually in corridors.html and exported to
# corridors.json. load_corridors() reads that file at import and RAISES if it
# is missing or malformed -- deliberately no fallback: a stale inline guess
# failing silently is more dangerous than refusing to start (the startup
# self-check in selfcheck.py reports the problem first).

# Routing tunables (full-res pixels).
SAMPLE_STEP = 150        # spacing of branch-off candidates along each spine edge
CONNECTOR_SLACK = 400    # accept visible connectors up to this much longer than the shortest
WALL_PENALTY = 1000      # cost multiplier for a segment that crosses a wall
# P5: walls count inflated by clearance in visibility/graph checks below.
# Same value and meaning as WALL_CLEARANCE_PX in src/test.py (kept in sync
# by hand; the test asserts inner route segments stay this far from walls).
WALL_CLEARANCE_PX = 6
# P5: endpoint epsilon — connectors may graze (not cross) walls within this
# distance of a route endpoint (room entries sit in door gaps by design).
# Must stay below the narrowest real doorway half-width (24.4px, 6F
# calib_report) so it can never punch through a wall beside a door.
ENDPOINT_EPS = 12


def _xy(v):
    return (v['x'], v['y']) if isinstance(v, dict) else (v[0], v[1])


def load_corridors():
    """Load the corridor spine from corridors.json.

    JSON shape: {"5F": {"map_file": ..., "waypoints": {id: [x, y]},
    "edges": [[a, b]], "walls": [[[x1, y1], [x2, y2]]]}}.
    Point values may be [x, y] lists or {x, y} dicts; both normalize to tuples.

    Raises FileNotFoundError if the file is missing and ValueError if it is
    malformed. Called once at import (fail fast) and again by
    _maybe_reload_corridors() when the file changes at runtime.
    """
    if not os.path.exists(CORRIDORS_FILE):
        raise FileNotFoundError(
            f"corridors.json not found at {CORRIDORS_FILE}. Author it in "
            "corridors.html; the server refuses to guess routes.")
    try:
        with open(CORRIDORS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (ValueError, OSError) as e:
        raise ValueError(f"corridors.json invalid: {e}")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("corridors.json has no floors")

    out = {}
    for floor, info in raw.items():
        if not isinstance(info, dict):
            raise ValueError(f"corridors[{floor}] is not an object")
        for key in ('map_file', 'waypoints', 'edges'):
            if key not in info:
                raise ValueError(f"corridors[{floor}] missing {key!r}")
        try:
            waypoints = {wid: _xy(v) for wid, v in info.get('waypoints', {}).items()}
            edges = [(a, b) for a, b in info.get('edges', [])]
            walls = [(_xy(p), _xy(q)) for p, q in info.get('walls', [])]
        except (TypeError, ValueError, KeyError, IndexError) as e:
            raise ValueError(f"corridors[{floor}] malformed: {e}")
        for a, b in edges:
            if a not in waypoints or b not in waypoints:
                raise ValueError(
                    f"corridors[{floor}] edge ({a!r}, {b!r}) "
                    "references unknown waypoint")
        out[floor] = {
            'map_file': info['map_file'],
            'waypoints': waypoints,
            'edges': edges,
            'walls': walls,
        }
    return out


CORRIDORS = load_corridors()
_CORRIDORS_MTIME = os.path.getmtime(CORRIDORS_FILE)


def _maybe_reload_corridors():
    """Hot-reload corridors.json if its mtime changed since the last load.

    A failed reload keeps the previous (working) data and logs a warning --
    it must never take down a serving process. The mtime is still advanced
    past the bad file so one broken edit warns once, not once per request.
    """
    global CORRIDORS, _CORRIDORS_MTIME
    try:
        mtime = os.path.getmtime(CORRIDORS_FILE)
    except OSError:
        return
    if mtime == _CORRIDORS_MTIME:
        return
    try:
        fresh = load_corridors()
    except (OSError, ValueError) as e:
        print(f"warning: failed to reload corridors.json ({e}), "
              "keeping previous data", file=sys.stderr)
        _CORRIDORS_MTIME = mtime
        return
    CORRIDORS = fresh
    _CORRIDORS_MTIME = mtime


def _acronym(name):
    """Build an acronym from the significant words of a room name.

    "Interdisciplinary Digital Research Lab (N507)" -> "IDRL".
    Parenthetical room codes and short filler words are ignored so the
    acronym reflects how patrons actually abbreviate the room.
    """
    stripped = re.sub(r'\(.*?\)', ' ', name)
    skip = {'OF', 'THE', 'AND', 'FOR', 'A', 'AN'}
    letters = [w[0] for w in stripped.split() if w and w.upper() not in skip]
    return ''.join(letters).upper()


def _call_key(call):
    """Split a call number into (letters, number) for ordered comparison.

    String comparison alone misorders call numbers ("A100" < "A9"), so we
    compare the alphabetic class first, then the numeric portion.
    """
    m = re.match(r'^([A-Z]+)\s*(\d*(?:\.\d+)?)', call.upper())
    if not m:
        return (call.upper(), 0.0)
    letters, num = m.group(1), m.group(2)
    return (letters, float(num) if num else 0.0)


def _call_id(row, key='call_start'):
    """Return a row's call id, upper-cased, or '' when blank/null."""
    v = (row.get(key) or '').strip().upper()
    return '' if v == 'NULL' else v


def _is_facility(row):
    return row['type'].lower() == 'facility'


def _base_name(row):
    """Facility name without its floor tag: "5F Restroom" -> "RESTROOM"."""
    return FLOOR_PREFIX_RE.sub('', row['name'].upper())


_ROWS_CACHE = {'mtime': None, 'ov_mtime': None, 'rows': None}


def _override_mtime():
    try:
        return os.path.getmtime(OVERRIDES_FILE)
    except OSError:
        return None


def _apply_overrides(rows):
    """Return rows with route_overrides.json ([place_id] -> [x, y]) applied.

    Invalid keys/values are ignored here on purpose: unknown-key failures are
    reported fail-fast by selfcheck at startup, and a bad deploy must not take
    down a serving process mid-session.
    """
    try:
        with open(OVERRIDES_FILE, mode='r', encoding='utf-8-sig') as f:
            ov = json.load(f)
    except (OSError, ValueError):
        return rows
    if not isinstance(ov, dict) or not ov:
        return rows
    id_to_idx = {pid: i for i, pid in enumerate(_assign_place_ids(rows))}
    for pid, target in ov.items():
        idx = id_to_idx.get(pid)
        if idx is None:
            continue
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            continue
        try:
            x = float(target[0])
            y = float(target[1])
        except (TypeError, ValueError):
            continue
        row = rows[idx]
        row['x'] = str(int(x)) if x == int(x) else repr(x)
        row['y'] = str(int(y)) if y == int(y) else repr(y)
    return rows


def _load_rows():
    """Read locations.csv (+route_overrides.json), hot-reloading on mtime change.

    The cache key covers both files' mtimes so editing the override alone
    triggers a refresh. Cached rows are the *effective* coordinates, shared by
    every consumer. A failed reload keeps the previous (working) rows and logs
    a warning. With no cache yet and no readable file, returns [] (the old
    missing-file behavior).
    """
    try:
        mtime = os.path.getmtime(DATA_FILE)
    except OSError:
        mtime = None
    ov_mtime = _override_mtime()
    cached = _ROWS_CACHE['rows']
    if (cached is not None and _ROWS_CACHE['mtime'] == mtime
            and _ROWS_CACHE['ov_mtime'] == ov_mtime):
        return [dict(r) for r in cached]
    rows = None
    if mtime is not None:
        try:
            with open(DATA_FILE, mode='r', encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error) as e:
            print(f"warning: failed to reload locations.csv ({e}), "
                  "keeping previous data", file=sys.stderr)
            _ROWS_CACHE['mtime'] = mtime
            _ROWS_CACHE['ov_mtime'] = ov_mtime
            return [dict(r) for r in cached] if cached is not None else []
    elif cached is not None:
        rows = cached
    if rows is not None:
        effective = _apply_overrides([dict(r) for r in rows])
        _ROWS_CACHE['mtime'] = mtime
        _ROWS_CACHE['ov_mtime'] = ov_mtime
        _ROWS_CACHE['rows'] = effective
    return [dict(r) for r in effective] if rows is not None else []


def _pick_nearest(candidates, near):
    """Choose among interchangeable facility rows: prefer `near`'s floor, then
    the shortest straight-line distance. Without `near`, keep CSV order."""
    if not near or len(candidates) == 1:
        return candidates[0]
    floor, x, y = near
    return min(candidates, key=lambda r: (
        r['floor'] != floor,
        (int(r['x']) - x) ** 2 + (int(r['y']) - y) ** 2,
    ))


def find_location(query, near=None):
    """Resolve a query to a single location row.

    `near` is an optional (floor, x, y). When the query matches a Facility that
    exists in several places (e.g. restrooms on 5F and 6F) the instance nearest
    `near` is returned; naming a floor ("6F restroom") pins that one.

    A stable place_id from search_locations (e.g. 'n607-6f') resolves
    exactly to its row before any fuzzy strategy runs; identity pins the
    instance, so `near` does not apply. Transit ids never resolve (routing
    infrastructure, not lookup targets).
    """
    raw_id = query.strip().lower()
    if raw_id:
        all_rows = _load_rows()
        for r, pid in zip(all_rows, _assign_place_ids(all_rows)):
            if pid == raw_id:
                if r['type'].lower() == 'transit':
                    return None
                return r
    query = query.strip().upper()
    for pattern, repl in SYNONYMS:
        query = pattern.sub(repl, query)

    # Transit rows (stairs/elevator) are routing infrastructure, not lookup
    # targets -- exclude them so a query never resolves to a stairwell.
    rows = [r for r in _load_rows() if r['type'].lower() != 'transit']
    if not rows:
        return None

    best_match = None
    highest_score = 0

    for row in rows:
        name = row['name'].upper()
        call_id = _call_id(row)

        # --- Strategy 1: Exact substring match (highest priority) ---
        if query in name or (call_id and query in call_id):
            if _is_facility(row):
                matches = [r for r in rows if _is_facility(r) and query in r['name'].upper()]
                return _pick_nearest(matches, near)
            return row

        # --- Strategy 2: Acronym match (e.g. "IDRL" -> full lab name) ---
        if len(query) >= 2 and query == _acronym(name):
            return row

        # --- Strategy 3: Fuzzy matching (handles typos) ---
        # Match against individual words in the name, e.g. split "Researcher Room (N607)"
        words = name.replace('(', ' ').replace(')', ' ').split()
        if call_id:
            words.append(call_id)

        for word in words:
            score = difflib.SequenceMatcher(None, query, word).ratio()
            if score > highest_score:
                highest_score = score
                best_match = row

    # Threshold of 0.7 to avoid false matches
    if highest_score > 0.7:
        if _is_facility(best_match):
            return _pick_nearest(facility_instances(best_match, rows), near)
        return best_match

    # --- Strategy 4: Call number range ---
    # Only attempt this when the query actually looks like a call number.
    # Otherwise an unrelated query (e.g. "printer") would land inside some
    # shelf's alphabetic range and wrongly resolve to that shelf.
    if CALL_NUMBER_RE.match(query):
        qkey = _call_key(query)
        for row in rows:
            if row['type'].lower() == 'shelf' and _call_id(row):
                end = _call_id(row, 'call_end') or _call_id(row)
                if _call_key(_call_id(row)) <= qkey <= _call_key(end):
                    return row

    return None


def facility_instances(row, rows=None):
    """All Facility rows that are the same kind as `row` (same name once the
    floor tag is stripped), including `row` itself. Non-facilities -> [row]."""
    if not _is_facility(row):
        return [row]
    rows = rows if rows is not None else _load_rows()
    same = [r for r in rows if _is_facility(r) and _base_name(r) == _base_name(row)]
    return same or [row]


def _other_floors_note(row):
    """" Also available on 6F." when the same facility exists on other floors."""
    floors = sorted({r['floor'] for r in facility_instances(row)} - {row['floor']})
    return f" Also available on {', '.join(floors)}." if floors else ""


def search_and_draw(query):
    """
    Main entry point: pin the location on the full floor map and return the
    annotated image as JPEG bytes along with a human-readable description.
    """
    location = find_location(query)
    if not location:
        raise ValueError(f"Location not found for '{query}'.")

    try:
        x, y = int(location['x']), int(location['y'])
        if ENABLE_MCP_APPS:
            from views import floor_for_map, render_view
            floor = floor_for_map(location['map_file'])
            if floor is not None:
                msg = (f"'{location['name']}' has been marked on the {location['floor']} floor map."
                       f"{_other_floors_note(location)}")
                return msg, render_view(floor, pin=(x, y))
        base_map_path = map_path(location['map_file'])

        if not os.path.exists(base_map_path):
            raise FileNotFoundError(f"Base map {base_map_path} not found. Please check the filename.")

        img = Image.open(base_map_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        draw = ImageDraw.Draw(img)

        # Draw a prominent marker on the full map
        radius = 60
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="red", outline="white", width=15)
        draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill="white")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        image_bytes = buf.getvalue()

        msg = (f"'{location['name']}' has been marked on the {location['floor']} floor map."
               f"{_other_floors_note(location)}")
        return msg, image_bytes

    except Exception as e:
        raise Exception(f"Failed to generate map: {str(e)}")


# --- Path finding / directions -------------------------------------------------

def _nearest_waypoint(floor, x, y):
    """Return (waypoint_id, (wx, wy)) on `floor` nearest to (x, y)."""
    wps = CORRIDORS[floor]["waypoints"]
    best_id, best_xy = min(
        wps.items(),
        key=lambda kv: (kv[1][0] - x) ** 2 + (kv[1][1] - y) ** 2,
    )
    return best_id, best_xy


def _project_to_segment(p, a, b):
    """Project point `p` onto segment a-b. Return (point_on_segment, dist2, t).

    `t` is the clamped parameter in [0, 1] (0 = at a, 1 = at b). Lets a route
    branch off a corridor at the closest point on an edge, not only at a node.
    """
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        t = 0.0
    else:
        t = ((px - ax) * dx + (py - ay) * dy) / seg2
        t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    return (qx, qy), (px - qx) ** 2 + (py - qy) ** 2, t


def _dist(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def _segments_cross(p1, p2, q1, q2):
    """True if segment p1-p2 properly intersects segment q1-q2.

    Touching at an endpoint or running collinear does not count, so a route
    may graze the end of a wall (a door jamb) without being blocked.
    """
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-9) - (v < -1e-9)

    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    return o1 * o2 < 0 and o3 * o4 < 0


def _blocked(floor, a, b):
    """True if the straight segment a-b crosses any wall drawn on `floor`."""
    return any(_segments_cross(a, b, w1, w2)
               for w1, w2 in CORRIDORS[floor].get("walls", []))


def _seg_wall_dist(floor, a, b):
    """Minimum distance from segment a-b to any wall on `floor`
    (proper crossings read as 0.0)."""
    best = float("inf")
    for w1, w2 in CORRIDORS[floor].get("walls", []):
        if _segments_cross(a, b, tuple(w1), tuple(w2)):
            return 0.0
        best = min(best, _pt_seg_dist(a, (tuple(w1)), (tuple(w2))),
                   _pt_seg_dist(b, (tuple(w1)), (tuple(w2))),
                   _pt_seg_dist(tuple(w1), a, b),
                   _pt_seg_dist(tuple(w2), a, b))
    return best


def _pt_seg_dist(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _blocked_clear(floor, a, b):
    """P5 clearance-aware block test: proper crossing OR approach within
    WALL_CLEARANCE_PX. Routing visibility and graph costs use this; the raw
    geometric crossing test stays in _blocked (tests assert on it)."""
    if _blocked(floor, a, b):
        return True
    return _seg_wall_dist(floor, a, b) < WALL_CLEARANCE_PX


def _blocked_beyond(floor, a, b, eps):
    """Like _blocked_clear but ignoring everything within `eps` of `a`
    (endpoint epsilon exemption: grazes near a room entry, never crossings
    further out — callers must still require a clear remainder)."""
    d = _dist(a, b)
    if d <= eps:
        return False
    t = eps / d
    c = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    return _blocked_clear(floor, c, b)


def _dijkstra(adj, start, end):
    """Shortest path over {node: [(neighbor, cost)]}; returns the node list or None."""
    dist = {start: 0.0}
    prev = {}
    heap = [(0.0, start)]
    while heap:
        d, node = heapq.heappop(heap)
        if node == end:
            path = [end]
            while path[-1] != start:
                path.append(prev[path[-1]])
            return path[::-1]
        if d > dist[node]:
            continue
        for nxt, cost in adj.get(node, []):
            nd = d + cost
            if nd < dist.get(nxt, float('inf')):
                dist[nxt] = nd
                prev[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return None


def _connector_penalized(floor, p, n):
    """True when the connector p->n was charged WALL_PENALTY by connect().

    Mirrors connect()'s admission rule exactly: a connector that crosses a
    wall is only ever used as the penalized fallback, and a connector that
    violates the clearance band is charged the penalty unless the violation
    sits inside ENDPOINT_EPS of the endpoint (the legitimate "you are
    standing in the doorway" case).
    """
    if _blocked(floor, p, n):
        return True
    return _blocked_clear(floor, p, n) and _blocked_beyond(floor, p, n,
                                                            ENDPOINT_EPS)


def _path_points(floor, start_xy, end_xy, diag=None):
    """Build the full route polyline on a single floor, respecting walls.

    The spine is densified: every edge is split at evenly spaced sample points
    and at the perpendicular projections of both endpoints. Each endpoint then
    connects to the spine points it can SEE (straight connector crossing no
    wall), keeping only connectors close in length to the shortest visible one
    so the route still leaves the corridor next to the room rather than cutting
    across open floor. Spine segments that cross a wall stay usable but carry a
    heavy penalty, so an incomplete wall set never disconnects the route.
    Dijkstra over this graph yields the path.

    The optional `diag` dict receives P5 penalty diagnostics (plan section
    2.4) without changing the return type: penalty_edges (C2: penalized
    inner segments plus one per endpoint fallback), end_fallback (C3),
    graph_fallback (C3b), epsilon_used. First/last polyline segments are
    excluded from the edge count, mirroring the test's inner-segment rule.
    """
    _maybe_reload_corridors()
    info = CORRIDORS[floor]
    wps = info["waypoints"]
    nodes = dict(wps)            # node id -> (x, y)
    group = {w: w for w in wps}  # node id -> the spine edge (or waypoint) it lies on
    adj = {}

    def link(a, b, cost):
        adj.setdefault(a, []).append((b, cost))
        adj.setdefault(b, []).append((a, cost))

    def seg_cost(p, q):
        d = _dist(p, q)
        return d * WALL_PENALTY if _blocked_clear(floor, p, q) else d

    # Densify spine edges into chains of short segments.
    seq = 0
    for ei, (a, b) in enumerate(info["edges"]):
        if a not in wps or b not in wps:
            continue
        pa, pb = wps[a], wps[b]
        length = _dist(pa, pb)
        ts = {i * SAMPLE_STEP / length for i in range(1, int(length // SAMPLE_STEP) + 1)} if length else set()
        for p in (start_xy, end_xy):
            ts.add(_project_to_segment(p, pa, pb)[2])
        chain = [a]
        for t in sorted(t for t in ts if 0 < t < 1):
            seq += 1
            nid = f"__s{seq}__"
            nodes[nid] = (pa[0] + t * (pb[0] - pa[0]), pa[1] + t * (pb[1] - pa[1]))
            group[nid] = ei
            chain.append(nid)
        chain.append(b)
        for u, v in zip(chain, chain[1:]):
            link(u, v, seg_cost(nodes[u], nodes[v]))

    spine_ids = [n for n in nodes if n in adj] or list(wps)

    def connect(anchor, xy):
        """Link `anchor` to the nearest visible point of each nearby spine edge.

        Returns (best, eps_used, fell_back): the shortest admitted connector
        length (or None), whether the endpoint-epsilon exemption was used,
        and whether nothing was even admissible (penalized fallback).

        Admission is proper-crossing-free (a room entry must reach the
        corridor through a doorway, grazing jambs if it must); clearance
        violations on this endpoint hop only raise its cost, mirroring the
        test's first/last-segment exemption. Sustained clearance discipline
        lives in seg_cost, not here. One connector per edge keeps the route
        from cutting diagonally along the corridor it joins.
        """
        nodes[anchor] = xy
        ranked = sorted(spine_ids, key=lambda n: _dist(xy, nodes[n]))
        best = None
        joined = set()
        eps_used = False
        for n in ranked:
            d = _dist(xy, nodes[n])
            if best is not None and d > best + CONNECTOR_SLACK:
                break
            if group.get(n) in joined:
                continue
            if _blocked(floor, xy, nodes[n]):
                continue
            if _blocked_clear(floor, xy, nodes[n]):
                if _blocked_beyond(floor, xy, nodes[n], ENDPOINT_EPS):
                    cost = d * WALL_PENALTY
                else:
                    cost = d
                    eps_used = True
            else:
                cost = d
            best = d if best is None else best
            joined.add(group.get(n))
            link(anchor, n, cost)
        fell_back = False
        if best is None and ranked:
            # Nothing admissible (walls enclose the point): fall back to the
            # nearest spine point, penalized like any wall crossing.
            n = ranked[0]
            link(anchor, n, _dist(xy, nodes[n]) * WALL_PENALTY)
            fell_back = True
        return best, eps_used, fell_back

    if not spine_ids:
        return [start_xy, end_xy] if start_xy != end_xy else [start_xy]

    s_best, s_eps, s_fell = connect("__start__", start_xy)
    e_best, e_eps, e_fell = connect("__end__", end_xy)

    # Two points closer to each other than to the corridor (e.g. neighbours in
    # the same room) may walk straight across when nothing blocks the way.
    direct = _dist(start_xy, end_xy)
    if (s_best is not None and e_best is not None and direct <= s_best + e_best
            and not _blocked_clear(floor, start_xy, end_xy)):
        link("__start__", "__end__", direct)

    route = _dijkstra(adj, "__start__", "__end__")
    graph_fallback = route is None
    if graph_fallback:
        route = ["__start__", "__end__"]
    pts = [nodes[n] for n in route]

    # Collapse duplicates and the collinear sample points along straight runs.
    out = [pts[0]]
    for p in pts[1:]:
        if p == out[-1]:
            continue
        if len(out) >= 2:
            (ax, ay), (bx, by) = out[-2], out[-1]
            cross = (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)
            dot = (bx - ax) * (p[0] - bx) + (by - ay) * (p[1] - by)
            if abs(cross) <= 1e-6 * max(1.0, _dist(out[-2], p) ** 2) and dot >= 0:
                out[-1] = p
                continue
        out.append(p)
    if diag is not None:
        segs = list(zip(out, out[1:]))
        # Count penalized segments over ALL of them, not just the inner ones.
        # The first and last polyline segments ARE the endpoint connectors
        # that connect() may have charged WALL_PENALTY, so restricting the
        # count to segs[1:-1] hid exactly the edges worth reporting -- and
        # the clearance assertion skipped the same segments, so neither side
        # of the test noticed.
        penalized = 0
        for i, (a, b) in enumerate(segs):
            if i == 0:
                if _connector_penalized(floor, a, b):
                    penalized += 1
            elif i == len(segs) - 1:
                if _connector_penalized(floor, b, a):
                    penalized += 1
            elif _blocked_clear(floor, a, b):
                penalized += 1
        diag.update({
            'penalty_edges': penalized + (1 if s_fell else 0)
            + (1 if e_fell else 0),
            'end_fallback': bool(s_fell or e_fell),
            'graph_fallback': bool(graph_fallback),
            'epsilon_used': bool(s_eps or e_eps),
        })
    return out


def _draw_route(map_file, points, start_color="green", end_color="red"):
    """Open `map_file`, draw the route polyline + endpoint markers, return JPEG.

    The polyline is drawn twice -- a wide white casing then a narrower blue core
    -- so it stays legible over a busy, light-colored floor plan. Endpoint
    markers reuse the ellipse geometry from search_and_draw().

    With ENABLE_MCP_APPS=1 this delegates to views.render_view (WebP, long
    side <= 1600); with the flag off the original full-res JPEG path below
    runs untouched (Step 6/L3 rollback: byte-identical to before).
    """
    if ENABLE_MCP_APPS:
        from views import floor_for_map, render_view
        floor = floor_for_map(map_file)
        if floor is not None:
            return render_view(floor, route=points,
                               start_color=start_color, end_color=end_color)
    base_map_path = map_path(map_file)
    if not os.path.exists(base_map_path):
        raise FileNotFoundError(f"Base map {base_map_path} not found. Please check the filename.")

    img = Image.open(base_map_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    if len(points) >= 2:
        draw.line(points, fill="white", width=34, joint="curve")
        draw.line(points, fill="#1E6FFF", width=18, joint="curve")

    radius = 60
    sx, sy = points[0]
    ex, ey = points[-1]
    draw.ellipse((sx - radius, sy - radius, sx + radius, sy + radius),
                 fill=start_color, outline="white", width=15)
    draw.ellipse((ex - radius, ey - radius, ex + radius, ey + radius),
                 fill=end_color, outline="white", width=15)
    draw.ellipse((ex - 15, ey - 15, ex + 15, ey + 15), fill="white")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _transit_options(floor):
    """Return the list of inter-floor transit rows (stairs/elevator) on `floor`.

    Transit points are tagged type=Transit in locations.csv and paired across
    floors by their call id (e.g. STAIRS_MAIN, STAIRS_WEST, ELEVATOR).
    """
    return [r for r in _load_rows()
            if r['type'].lower() == 'transit' and r['floor'] == floor]


def _nearest_transit(from_floor, to_floor, x, y, accessible_only=False):
    """Pick the transit structure (stairs/elevator) nearest (x, y) on
    `from_floor`, returning (from_row, to_row) for the same structure on the
    destination floor. Returns (None, None) if none is available on both floors.

    With accessible_only=True, only the elevator is considered.
    """
    starts = _transit_options(from_floor)
    ends = {r['call_start']: r for r in _transit_options(to_floor)}
    candidates = [(s, ends[s['call_start']]) for s in starts if s['call_start'] in ends]
    if accessible_only:
        candidates = [(s, e) for s, e in candidates
                      if s['call_start'] == 'ELEVATOR']
    if not candidates:
        return None, None
    return min(
        candidates,
        key=lambda pair: (int(pair[0]['x']) - x) ** 2 + (int(pair[0]['y']) - y) ** 2,
    )


def build_route(destination, start=None, accessible_only=False):
    """Compute a walking route and return it as plain data (no images).

    `start` defaults to the library Entrance & Exit. With accessible_only=True
    only the elevator is used for floor changes. Returns route_data::

        {'src': src_row, 'dest': dest_row,
         'kind': 'already_there' | 'same_floor' | 'cross_floor',
         'legs': [{'floor', 'map_file', 'points',
                   'start_color', 'end_color'}, ...],
         'transit': {'name', 'from_floor', 'to_floor', 'direction'} | None,
         'msg': text_msg}

    get_directions() renders the legs to JPEG; Step 3 serializes this dict.
    Raises ValueError if either endpoint cannot be resolved.
    """
    _maybe_reload_corridors()

    if start:
        src = find_location(start)
        if not src:
            raise ValueError(f"Start not found for '{start}'.")
    else:
        src = find_location("Entrance")
        if not src:
            raise ValueError("Default start 'Entrance & Exit' not found in locations.csv.")

    s_floor = src['floor']
    s_xy = (int(src['x']), int(src['y']))

    dest = find_location(destination, near=(s_floor, *s_xy))
    if not dest:
        raise ValueError(f"Destination not found for '{destination}'.")

    d_floor = dest['floor']
    d_xy = (int(dest['x']), int(dest['y']))

    # Already there.
    if src['name'] == dest['name']:
        msg = f"You're already at '{dest['name']}' on the {d_floor} floor."
        return {'src': src, 'dest': dest, 'kind': 'already_there',
                'legs': [{'floor': d_floor,
                          'map_file': CORRIDORS[d_floor]["map_file"],
                          'points': [d_xy, d_xy],
                          'start_color': 'red', 'end_color': 'red',
                          'diag': {'penalty_edges': 0, 'end_fallback': False,
                                   'graph_fallback': False,
                                   'epsilon_used': False}}],
                'transit': None, 'msg': msg}

    # Same floor: a single routed map. State plainly that the trip stays on one
    # floor so the answer never invents a stairs/elevator step.
    if s_floor == d_floor:
        diag1 = {}
        pts = _path_points(s_floor, s_xy, d_xy, diag1)
        msg = (f"'{src['name']}' and '{dest['name']}' are both on the {s_floor} "
               f"floor, so this is a single-floor walk -- no stairs or elevator "
               f"and no floor change are needed. Follow the route on the {s_floor} "
               f"map: green marker = start, red marker = destination.")
        return {'src': src, 'dest': dest, 'kind': 'same_floor',
                'legs': [{'floor': s_floor,
                          'map_file': CORRIDORS[s_floor]["map_file"],
                          'points': pts,
                          'start_color': 'green', 'end_color': 'red',
                          'diag': diag1}],
                'transit': None, 'msg': msg}

    # Cross floor: route start -> nearest transit, then transit -> destination.
    # The transit structure (stairs/elevator) is chosen by distance from the
    # start, and the SAME structure is used to arrive on the destination floor.
    s_transit, d_transit = _nearest_transit(s_floor, d_floor, *s_xy,
                                            accessible_only=accessible_only)
    if not s_transit:
        raise ValueError(
            f"No transit (stairs/elevator) defined between {s_floor} and {d_floor}; "
            f"add Transit rows to locations.csv."
        )
    transit_name = s_transit['name']
    s_transit_xy = (int(s_transit['x']), int(s_transit['y']))
    d_transit_xy = (int(d_transit['x']), int(d_transit['y']))

    diag1, diag2 = {}, {}
    pts1 = _path_points(s_floor, s_xy, s_transit_xy, diag1)
    pts2 = _path_points(d_floor, d_transit_xy, d_xy, diag2)

    direction = "up" if d_floor > s_floor else "down"
    msg = (f"'{src['name']}' is on {s_floor} and '{dest['name']}' is on {d_floor}, "
           f"so this trip changes floors using the {transit_name} (the same "
           f"{transit_name} connects both floors). "
           f"Step 1 ({s_floor} map): from the start (green marker), follow the route "
           f"to the {transit_name} (red marker). "
           f"Step 2: take the {transit_name} {direction} to {d_floor}. "
           f"Step 3 ({d_floor} map): from the {transit_name} (green marker), follow "
           f"the route to '{dest['name']}' (red marker). "
           f"Do not mention any other stairs or elevator -- use only the {transit_name}.")
    return {'src': src, 'dest': dest, 'kind': 'cross_floor',
            'legs': [{'floor': s_floor,
                      'map_file': CORRIDORS[s_floor]["map_file"],
                      'points': pts1,
                      'start_color': 'green', 'end_color': 'red',
                      'diag': diag1},
                     {'floor': d_floor,
                      'map_file': CORRIDORS[d_floor]["map_file"],
                      'points': pts2,
                      'start_color': 'green', 'end_color': 'red',
                      'diag': diag2}],
            'transit': {'name': transit_name, 'from_floor': s_floor,
                        'to_floor': d_floor, 'direction': direction},
            'msg': msg}


def get_directions(destination, start=None):
    """Generate walking directions and return (text_msg, [(floor, jpeg_bytes), ...]).

    `start` defaults to the library Entrance & Exit. Same-floor trips return one
    annotated map; cross-floor trips route via the stairs and return one map per
    floor. A facility destination (restroom, printer, ...) resolves to the
    instance nearest the start. Raises ValueError if either endpoint cannot be
    resolved.
    """
    route = build_route(destination, start)
    images = [(leg['floor'],
               _draw_route(leg['map_file'], leg['points'],
                           start_color=leg['start_color'],
                           end_color=leg['end_color']))
              for leg in route['legs']]
    return route['msg'], images


# --- Structured data API (plan Step 3) ---------------------------------------
# Pure-data counterparts of the image tools. find_location()/get_directions()
# above are frozen (byte-level); the matcher below re-implements the same four
# strategies in candidate-collecting form. The duplication is deliberate:
# zero regression risk to the frozen paths.


def _place_id(row):
    """Stable place id: lowercased call_start + floor suffix (n607-6f), or the
    normalized name when there is no call number."""
    key = (row.get('call_start') or '').strip()
    floor = (row.get('floor') or '').strip().lower()
    if key and key.upper() != 'NULL':
        base = key.lower()
    else:
        base = re.sub(r'[^a-z0-9]+', '-',
                      (row.get('name') or '').lower()).strip('-')
    return f'{base}-{floor}' if floor else base


def _assign_place_ids(rows):
    """Deterministic globally-unique place ids for a row list, CSV order.

    Upstream data reuses call_starts ("PN") and facility names ("5F Fire
    Exit"), so base ids collide; later duplicates get -2/-3/... suffixes.
    Stable for a given file content; only the colliding group shifts when
    the CSV changes.
    """
    counts = {}
    ids = []
    for r in rows:
        base = _place_id(r)
        n = counts.get(base, 0) + 1
        counts[base] = n
        ids.append(base if n == 1 else f"{base}-{n}")
    return ids


def _row_to_place(row, place_id=None):
    call = (row.get('call_start') or '').strip() or None
    if call is not None and call.upper() == 'NULL':
        call = None
    return models.Place(
        id=place_id or _place_id(row),
        name=row.get('name', ''),
        type=row.get('type', ''),
        floor=row.get('floor', ''),
        point=[int(row['x']), int(row['y'])],
        call_number=call,
        other_floors=sorted(
            {r['floor'] for r in facility_instances(row)}
            - {row.get('floor')}),
    )


def _match_candidates(query, rows=None):
    """All rows matching `query` as (all_rows_index, row, exact, score).

    Same four strategies as find_location, but collecting every candidate
    instead of first-hit: exact substring/acronym/call-range hits get
    exact=True; fuzzy hits carry their best word score (> 0.7). The index
    keys into the full (transit-included) row list so callers can align
    stable place ids via _assign_place_ids on the same list.
    """
    q = query.strip().upper()
    for pattern, repl in SYNONYMS:
        q = pattern.sub(repl, q)
    all_rows = rows if rows is not None else _load_rows()
    indexed = [(j, r) for j, r in enumerate(all_rows)
               if r['type'].lower() != 'transit']
    out = []
    fuzzy_best = 0.0
    for i, r in indexed:
        name = r['name'].upper()
        call_id = _call_id(r)
        if q in name or (call_id and q in call_id):
            out.append((i, r, True, 1.0))
            continue
        if len(q) >= 2 and q == _acronym(name):
            out.append((i, r, True, 1.0))
            continue
        words = name.replace('(', ' ').replace(')', ' ').split()
        if call_id:
            words.append(call_id)
        best = max((difflib.SequenceMatcher(None, q, w).ratio()
                    for w in words), default=0.0)
        fuzzy_best = max(fuzzy_best, best)
        if best > 0.7:
            out.append((i, r, False, best))
    # Strategy 4 (call-number range) runs only when nothing better matched,
    # mirroring find_location, where it is unreachable after any exact hit
    # or any fuzzy score above 0.7.
    if not any(e for _, _, e, _ in out) and fuzzy_best <= 0.7 \
            and CALL_NUMBER_RE.match(q):
        qkey = _call_key(q)
        for i, r in indexed:
            if r['type'].lower() == 'shelf' and _call_id(r):
                end = _call_id(r, 'call_end') or _call_id(r)
                if _call_key(_call_id(r)) <= qkey <= _call_key(end):
                    out.append((i, r, True, 1.0))
    return out


def search_locations(query, limit=5, types=None, near_floor=None):
    """Search locations, returning a ranked candidate list (RoutePlan inputs).

    Ranking is deterministic: exact hits in CSV order, then fuzzy hits by
    score desc (CSV order breaks ties). `types` hard-filters (e.g.
    ["Room"]); `near_floor` ("5F"/"6F") stably boosts same-floor hits.
    Ids come from one _assign_place_ids pass over a single row load, so
    search/detail always agree, even for duplicated base ids.
    """
    rows = _load_rows()
    idmap = _assign_place_ids(rows)
    cands = _match_candidates(query, rows)
    if types:
        wanted = {str(t).lower() for t in types}
        cands = [c for c in cands if c[1].get('type', '').lower() in wanted]
    total = len(cands)
    exact = sum(1 for c in cands if c[2])
    ranked = [c for c in cands if c[2]] + sorted(
        (c for c in cands if not c[2]), key=lambda c: (-c[3], c[0]))
    if near_floor:
        nf = str(near_floor).upper()
        ranked = sorted(
            ranked, key=lambda c: c[1].get('floor', '').upper() != nf)
    picked = ranked[:max(1, int(limit))]
    return models.LocationSearchResult(
        query=query,
        matches=[models.LocationMatch(place=_row_to_place(r, idmap[j]),
                                      exact=e, score=s)
                 for j, r, e, s in picked],
        total_matches=total,
        exact_matches=exact)


def get_location_detail(place_id):
    """Full record for one place id (as returned by search_locations).

    Unknown ids fail loudly AND point the way out: the error names the
    closest matches and tells the caller to use search_locations, so a
    model that passes a name ("N607") instead of an id ("n607-6f") can
    recover in one more call instead of guessing.
    """
    rows = _load_rows()
    idmap = _assign_place_ids(rows)
    for r, pid in zip(rows, idmap):
        if pid == place_id:
            return _row_to_place(r, pid)
    hints = _match_candidates(place_id, rows)[:3]
    if hints:
        sug = "; ".join(
            f"{idmap[j]} ({r['name']})" for j, r, _, _ in hints)
        raise ValueError(
            f"Unknown place id '{place_id}'. Did you mean: {sug}? "
            "Call search_locations to list valid ids.")
    raise ValueError(
        f"Unknown place id '{place_id}'. "
        "Call search_locations to list valid ids.")


_MAP_SIZES = {}


def _map_size(floor):
    if floor not in _MAP_SIZES:
        path = map_path(CORRIDORS[floor]['map_file'])
        with Image.open(path) as img:
            _MAP_SIZES[floor] = [img.width, img.height]
    return _MAP_SIZES[floor]


def _leg_steps(kind, leg, origin, dest, transit):
    """Per-leg human steps. Only start/transit/end kinds are emitted in
    Step 3 (no door/corridor segmentation data exists yet); the schema
    already accepts the finer kinds for Step 5."""
    pts = leg['points']
    if kind == 'already_there':
        return [models.RouteStep(
            kind='end', point=[float(dest['x']), float(dest['y'])],
            place_id=_place_id(dest), label=dest['name'],
            instruction=f"You're already at '{dest['name']}' "
                        f"on the {dest['floor']} floor.")]
    if kind == 'same_floor':
        return [
            models.RouteStep(
                kind='start', point=[float(p) for p in pts[0]],
                place_id=_place_id(origin), label=origin['name'],
                instruction=f"Start at {origin['name']}."),
            models.RouteStep(
                kind='end', point=[float(p) for p in pts[-1]],
                place_id=_place_id(dest), label=dest['name'],
                instruction=f"Arrive at {dest['name']}."),
        ]
    # Cross floor: leg 0 ends at the transit, leg 1 starts from it.
    starts_here = (pts[0][0] == int(origin['x'])
                   and pts[0][1] == int(origin['y']))
    if starts_here:
        return [
            models.RouteStep(
                kind='start', point=[float(p) for p in pts[0]],
                place_id=_place_id(origin), label=origin['name'],
                instruction=f"Start at {origin['name']}."),
            models.RouteStep(
                kind='transit', point=[float(p) for p in pts[-1]],
                place_id=None, label=transit['name'],
                instruction=f"Take the {transit['name']} "
                            f"{transit['direction']} to {transit['to_floor']}."),
        ]
    return [
        models.RouteStep(
            kind='transit', point=[float(p) for p in pts[0]],
            place_id=None, label=transit['name'],
            instruction=f"Arrive on {transit['to_floor']} "
                        f"at the {transit['name']}."),
        models.RouteStep(
            kind='end', point=[float(p) for p in pts[-1]],
            place_id=_place_id(dest), label=dest['name'],
            instruction="You have arrived."),
    ]


def _polyline_length(pts):
    return sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
               for a, b in zip(pts, pts[1:]))


def get_route(destination, start=None, accessible_only=False,
              view_mode='3d'):
    """Walking route as a RoutePlan (pure JSON, no images).

    view_mode picks how the interactive view presents the plan: '3d' (the
    default) renders an extruded model of the building with the route
    floating above the wall tops; '2d' renders a flat floor plan.
    """
    route = build_route(destination, start,
                        accessible_only=accessible_only)
    origin = _row_to_place(route['src'])
    dest = _row_to_place(route['dest'])
    legs = []
    for leg in route['legs']:
        poly = [[float(x), float(y)] for x, y in leg['points']]
        transit = None
        if route['transit'] is not None:
            t = route['transit']
            transit = models.TransitInfo(
                id=re.sub(r'[^a-z0-9]+', '-', t['name'].lower()).strip('-'),
                name=t['name'], from_floor=t['from_floor'],
                to_floor=t['to_floor'], direction=t['direction'],
                accessible='elevator' in t['name'].lower())
        legs.append(models.RouteLeg(
            floor=leg['floor'], map_file=leg['map_file'],
            map_size=_map_size(leg['floor']), polyline=poly,
            steps=_leg_steps(route['kind'], leg, route['src'], route['dest'],
                             route['transit']),
            transit=(transit if leg == route['legs'][0]
                     and route['kind'] == 'cross_floor' else None)))
    names = [route['transit']['name']] if route['transit'] else []
    totals = models.RouteTotals(
        distance_px=sum(_polyline_length(leg['points'])
                        for leg in route['legs']),
        floor_changes=len(legs) - 1,
        step_free=all('elevator' in n.lower() for n in names),
        transits_used=names)
    warnings = []
    for leg in legs:
        if not CORRIDORS[leg.floor].get('walls'):
            warnings.append(
                f"{leg.floor} has no wall segments marked; "
                "the route may pass through walls")
    plan_id = hashlib.sha1(
        f"{origin.id}:{dest.id}:{accessible_only}".encode()).hexdigest()[:12]
    return models.RoutePlan(
        plan_id=plan_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        origin=origin, destination=dest, totals=totals, legs=legs,
        warnings=warnings, view_mode=view_mode)


STATUS_FILE = os.path.join(BASE_DIR, 'status.json')


def get_library_status(floor=None):
    """Closure/status layer (design placeholder per plan).

    Reads the optional status.json
    ({"source": ..., "closures": [{"floor", "area", "reason", "until"}]}).
    Missing file -> source "none" + empty list. No network, by design.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATUS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return models.LibraryStatus(source='none', closures=[],
                                    updated_at=now)
    closures = []
    for c in raw.get('closures', []):
        if (floor is not None and c.get('floor') is not None
                and c['floor'] != floor):
            continue
        closures.append(models.Alert(
            floor=c.get('floor'), area=c.get('area', ''),
            reason=c.get('reason', ''), until=c.get('until')))
    return models.LibraryStatus(
        source=raw.get('source', 'status.json'), closures=closures,
        updated_at=now)
