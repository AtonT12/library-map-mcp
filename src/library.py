import csv
import heapq
import io
import json
from PIL import Image, ImageDraw
import os
import re
import difflib

# Config: absolute path based on project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, 'locations.csv')
CORRIDORS_FILE = os.path.join(BASE_DIR, 'corridors.json')

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
# corridors.json; load_corridors() reads that file at import. The dict below is
# only a fallback used when corridors.json is missing or unreadable.
_FALLBACK_CORRIDORS = {
    "5F": {
        "map_file": "5f_base.jpg",
        "waypoints": {
            "ent": (4065, 805),     # top corridor, above the Entrance & Exit door
            "top_w": (1500, 805),   # west end of the top corridor
            "top_e": (5400, 805),   # east end of the top corridor
            "ww_top": (300, 1540),  # west wing, top of the left-edge corridor
            "ww_mid": (300, 2500),  # west wing, middle (W508-W517 column)
            "ww_bot": (300, 3700),  # west wing, bottom (W518/W522 row)
            "nrow": (3360, 1497),   # N51x classroom row
        },
        "edges": [
            ("ent", "top_w"), ("ent", "top_e"), ("ent", "nrow"),
            ("top_w", "ww_top"),
            ("ww_top", "ww_mid"), ("ww_mid", "ww_bot"),
        ],
        "walls": [],
    },
    "6F": {
        "map_file": "6f_base.jpg",
        "waypoints": {
            "stair": (770, 900),       # near the 6F staircase
            "main": (4000, 900),       # central main-collection corridor
            "gsr": (3100, 1483),       # group study room row (N601-N605)
            "scholars": (6400, 700),   # east wing toward Scholars Space
        },
        "edges": [
            ("stair", "main"), ("main", "gsr"), ("main", "scholars"),
        ],
        "walls": [],
    },
}

# Routing tunables (full-res pixels).
SAMPLE_STEP = 150        # spacing of branch-off candidates along each spine edge
CONNECTOR_SLACK = 400    # accept visible connectors up to this much longer than the shortest
WALL_PENALTY = 1000      # cost multiplier for a segment that crosses a wall


def _xy(v):
    return (v['x'], v['y']) if isinstance(v, dict) else (v[0], v[1])


def load_corridors():
    """Load the corridor spine from corridors.json, falling back to the inline
    dict if the file is missing or malformed.

    JSON shape: {"5F": {"map_file": ..., "waypoints": {id: [x, y]},
    "edges": [[a, b]], "walls": [[[x1, y1], [x2, y2]]]}}.
    Point values may be [x, y] lists or {x, y} dicts; both normalize to tuples.
    """
    if not os.path.exists(CORRIDORS_FILE):
        return _FALLBACK_CORRIDORS
    try:
        with open(CORRIDORS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (ValueError, OSError):
        return _FALLBACK_CORRIDORS

    out = {}
    for floor, info in raw.items():
        out[floor] = {
            'map_file': info['map_file'],
            'waypoints': {wid: _xy(v) for wid, v in info.get('waypoints', {}).items()},
            'edges': [(a, b) for a, b in info.get('edges', [])],
            'walls': [(_xy(p), _xy(q)) for p, q in info.get('walls', [])],
        }
    return out or _FALLBACK_CORRIDORS


CORRIDORS = load_corridors()


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


def _load_rows():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, mode='r', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


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
    """
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
        base_map_path = os.path.join(BASE_DIR, location['map_file'])

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


def _path_points(floor, start_xy, end_xy):
    """Build the full route polyline on a single floor, respecting walls.

    The spine is densified: every edge is split at evenly spaced sample points
    and at the perpendicular projections of both endpoints. Each endpoint then
    connects to the spine points it can SEE (straight connector crossing no
    wall), keeping only connectors close in length to the shortest visible one
    so the route still leaves the corridor next to the room rather than cutting
    across open floor. Spine segments that cross a wall stay usable but carry a
    heavy penalty, so an incomplete wall set never disconnects the route.
    Dijkstra over this graph yields the path.
    """
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
        return d * WALL_PENALTY if _blocked(floor, p, q) else d

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
        """Link `anchor` to the nearest visible point of each nearby spine edge;
        return the shortest visible connector length (or None).

        One connector per edge keeps the route from cutting diagonally along
        the corridor it joins; several edges let it pick the right corridor.
        """
        nodes[anchor] = xy
        ranked = sorted(spine_ids, key=lambda n: _dist(xy, nodes[n]))
        best = None
        joined = set()
        for n in ranked:
            d = _dist(xy, nodes[n])
            if best is not None and d > best + CONNECTOR_SLACK:
                break
            if group.get(n) in joined:
                continue
            if not _blocked(floor, xy, nodes[n]):
                best = d if best is None else best
                joined.add(group.get(n))
                link(anchor, n, d)
        if best is None and ranked:
            # Nothing visible (walls enclose the point): fall back to the
            # nearest spine point, penalized like any wall crossing.
            n = ranked[0]
            link(anchor, n, _dist(xy, nodes[n]) * WALL_PENALTY)
        return best

    if not spine_ids:
        return [start_xy, end_xy] if start_xy != end_xy else [start_xy]

    s_best = connect("__start__", start_xy)
    e_best = connect("__end__", end_xy)

    # Two points closer to each other than to the corridor (e.g. neighbours in
    # the same room) may walk straight across when nothing blocks the way.
    direct = _dist(start_xy, end_xy)
    if (s_best is not None and e_best is not None and direct <= s_best + e_best
            and not _blocked(floor, start_xy, end_xy)):
        link("__start__", "__end__", direct)

    route = _dijkstra(adj, "__start__", "__end__") or ["__start__", "__end__"]
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
    return out


def _draw_route(map_file, points, start_color="green", end_color="red"):
    """Open `map_file`, draw the route polyline + endpoint markers, return JPEG.

    The polyline is drawn twice -- a wide white casing then a narrower blue core
    -- so it stays legible over a busy, light-colored floor plan. Endpoint
    markers reuse the ellipse geometry from search_and_draw().
    """
    base_map_path = os.path.join(BASE_DIR, map_file)
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
    floors by their call id (e.g. STAIRS_CENTRAL, ELEVATOR).
    """
    return [r for r in _load_rows()
            if r['type'].lower() == 'transit' and r['floor'] == floor]


def _nearest_transit(from_floor, to_floor, x, y):
    """Pick the transit structure (stairs/elevator) nearest (x, y) on
    `from_floor`, returning (from_row, to_row) for the same structure on the
    destination floor. Returns (None, None) if none is available on both floors.
    """
    starts = _transit_options(from_floor)
    ends = {r['call_start']: r for r in _transit_options(to_floor)}
    candidates = [(s, ends[s['call_start']]) for s in starts if s['call_start'] in ends]
    if not candidates:
        return None, None
    return min(
        candidates,
        key=lambda pair: (int(pair[0]['x']) - x) ** 2 + (int(pair[0]['y']) - y) ** 2,
    )


def get_directions(destination, start=None):
    """Generate walking directions and return (text_msg, [(floor, jpeg_bytes), ...]).

    `start` defaults to the library Entrance & Exit. Same-floor trips return one
    annotated map; cross-floor trips route via the stairs and return one map per
    floor. A facility destination (restroom, printer, ...) resolves to the
    instance nearest the start. Raises ValueError if either endpoint cannot be
    resolved.
    """
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
        img = _draw_route(CORRIDORS[d_floor]["map_file"], [d_xy, d_xy],
                          start_color="red", end_color="red")
        msg = f"You're already at '{dest['name']}' on the {d_floor} floor."
        return msg, [(d_floor, img)]

    # Same floor: a single routed map. State plainly that the trip stays on one
    # floor so the answer never invents a stairs/elevator step.
    if s_floor == d_floor:
        pts = _path_points(s_floor, s_xy, d_xy)
        img = _draw_route(CORRIDORS[s_floor]["map_file"], pts)
        msg = (f"'{src['name']}' and '{dest['name']}' are both on the {s_floor} "
               f"floor, so this is a single-floor walk -- no stairs or elevator "
               f"and no floor change are needed. Follow the route on the {s_floor} "
               f"map: green marker = start, red marker = destination.")
        return msg, [(s_floor, img)]

    # Cross floor: route start -> nearest transit, then transit -> destination.
    # The transit structure (stairs/elevator) is chosen by distance from the
    # start, and the SAME structure is used to arrive on the destination floor.
    s_transit, d_transit = _nearest_transit(s_floor, d_floor, *s_xy)
    if not s_transit:
        raise ValueError(
            f"No transit (stairs/elevator) defined between {s_floor} and {d_floor}; "
            f"add Transit rows to locations.csv."
        )
    transit_name = s_transit['name']
    s_transit_xy = (int(s_transit['x']), int(s_transit['y']))
    d_transit_xy = (int(d_transit['x']), int(d_transit['y']))

    pts1 = _path_points(s_floor, s_xy, s_transit_xy)
    img1 = _draw_route(CORRIDORS[s_floor]["map_file"], pts1,
                       start_color="green", end_color="red")
    pts2 = _path_points(d_floor, d_transit_xy, d_xy)
    img2 = _draw_route(CORRIDORS[d_floor]["map_file"], pts2,
                       start_color="green", end_color="red")

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
    return msg, [(s_floor, img1), (d_floor, img2)]
