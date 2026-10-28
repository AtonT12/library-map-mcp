import math
import os

import library


def quick_test():
    target = "Resercher"  # Replace with any name or call number from locations.csv
    print(f"--- Testing search: {target} ---")

    try:
        msg, image_bytes = library.search_and_draw(target)
        print(f"Success! Result: {msg}")
        print(f"Image bytes: {len(image_bytes)}")
    except Exception as e:
        print(f"Failed: {e}")


def directions_test():
    print("\n--- Testing directions ---")

    # 1. Same-floor: Entrance (5F) -> W522 (5F).
    msg, images = library.get_directions("W522")
    assert len(images) == 1, f"same-floor should be 1 image, got {len(images)}"
    assert images[0][0] == "5F" and len(images[0][1]) > 0
    assert "5F" in msg
    print(f"[ok] same-floor: {len(images)} image, {len(images[0][1])} bytes")

    # 2. Cross-floor: Entrance (5F) -> N607 (6F).
    msg, images = library.get_directions("N607")
    assert len(images) == 2, f"cross-floor should be 2 images, got {len(images)}"
    assert {f for f, _ in images} == {"5F", "6F"}
    assert "stairs" in msg.lower()
    print(f"[ok] cross-floor: {len(images)} images, floors {[f for f, _ in images]}")

    # 3. Explicit start, both 5F.
    msg, images = library.get_directions("N509", start="W516")
    assert len(images) == 1 and images[0][0] == "5F"
    print(f"[ok] explicit start W516 -> N509: {len(images)} image")

    # 4. Call-number destination still resolves via find_location.
    msg, images = library.get_directions("QA76")
    assert len(images) >= 1
    print(f"[ok] call-number dest QA76: {len(images)} image(s)")

    # 5. Errors raise ValueError.
    for bad in [("nonexistent", None), ("N607", "nonexistent")]:
        try:
            library.get_directions(bad[0], bad[1])
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass
    print("[ok] unknown start/destination raise ValueError")

    # 6. Unit checks on the spine helpers (name-agnostic: waypoint ids come
    #    from corridors.json and may be arbitrary like wp1/wp2).
    wps = library.CORRIDORS["5F"]["waypoints"]
    assert wps, "no 5F waypoints loaded from corridors.json"
    near_id, near_xy = library._nearest_waypoint("5F", 4065, 805)
    assert near_id in wps and near_xy == wps[near_id]
    pts = library._path_points("5F", (3360, 805), (700, 3200))
    assert pts[0] == (3360, 805) and pts[-1] == (700, 3200)
    assert all(pts[i] != pts[i + 1] for i in range(len(pts) - 1)), "duplicate consecutive points"
    # Cross-floor transit picks the structure nearest the start, same one on arrival.
    s_t, d_t = library._nearest_transit("5F", "6F", 4065, 1270)  # from entrance
    assert s_t and d_t and s_t["call_start"] == d_t["call_start"]
    assert library._transit_options("6F"), "no 6F transit rows"
    print(f"[ok] unit checks: route has {len(pts)} points; nearest transit from entrance = {s_t['name']}")

    print("All directions tests passed.")


def walls_test():
    print("\n--- Testing walls ---")
    assert library._segments_cross((0, 0), (10, 10), (0, 10), (10, 0))
    assert not library._segments_cross((0, 0), (10, 0), (0, 5), (10, 5))
    assert not library._segments_cross((0, 0), (10, 0), (10, 0), (10, 10)), "touching endpoint is not a crossing"

    floor = library.CORRIDORS["5F"]
    saved = floor.get("walls", [])
    start, end = (4065, 1270), (440, 3780)  # Entrance -> W522
    try:
        floor["walls"] = []
        open_pts = library._path_points("5F", start, end)

        # A wall between W522 and the corridor it used to branch off from.
        last_leg = (open_pts[-2], open_pts[-1])
        mx, my = (last_leg[0][0] + last_leg[1][0]) / 2, (last_leg[0][1] + last_leg[1][1]) / 2
        floor["walls"] = [((mx, my - 600), (mx, my + 600)), ((mx - 600, my), (mx + 600, my))]
        pts = library._path_points("5F", start, end)
        assert pts[0] == start and pts[-1] == end
        for a, b in zip(pts, pts[1:]):
            assert not library._blocked("5F", a, b), f"route segment {a}->{b} crosses a wall"
        assert pts != open_pts
        print(f"[ok] route avoids a wall: {len(open_pts)} -> {len(pts)} points")

        # A destination sealed inside walls still gets a route (penalized fallback).
        x, y = end
        floor["walls"] = [((x - 50, y - 50), (x + 50, y - 50)), ((x + 50, y - 50), (x + 50, y + 50)),
                          ((x + 50, y + 50), (x - 50, y + 50)), ((x - 50, y + 50), (x - 50, y - 50))]
        pts = library._path_points("5F", start, end)
        assert pts[0] == start and pts[-1] == end
        print("[ok] enclosed destination still routes")
    finally:
        floor["walls"] = saved


def facilities_test():
    print("\n--- Testing facilities ---")
    for q in ["WC", "toilet", "bathroom", "restroom"]:
        r = library.find_location(q)
        assert r and r["type"] == "Facility" and "Restroom" in r["name"], (q, r)
    assert library.find_location("6F restroom")["floor"] == "6F"
    assert library.find_location("water")["name"] == "5F Water Dispenser"
    assert "Printer" in library.find_location("print")["name"]
    r = library.find_location("N")
    assert r and r["call_start"].lower() != "null" and r["type"] != "Facility"

    msg, images = library.get_directions("restroom", start="N607")
    assert "6F Restroom" in msg and len(images) == 1 and images[0][0] == "6F", msg
    msg, images = library.get_directions("water")
    assert "5F Water Dispenser" in msg and len(images) == 1
    msg, _ = library.search_and_draw("WC")
    assert "Also available on 6F" in msg, msg
    print("[ok] synonyms, nearest-instance and other-floor note")


def build_route_test():
    print("\n--- Testing build_route ---")
    r = library.build_route("N607")
    assert r["kind"] == "cross_floor"
    assert [leg["floor"] for leg in r["legs"]] == ["5F", "6F"]
    assert r["transit"]["name"] == "Central Stairs"
    msg, images = library.get_directions("N607")
    assert msg == r["msg"], "render wrapper must not alter text"
    for leg, (floor, _) in zip(r["legs"], images):
        assert leg["floor"] == floor
    ra = library.build_route("N607", accessible_only=True)
    assert ra["transit"]["name"] == "Elevator", ra["transit"]
    same = library.build_route("W522")
    assert same["kind"] == "same_floor" and len(same["legs"]) == 1
    same_msg, _ = library.get_directions("W522")
    assert same_msg == same["msg"]
    already = library.build_route("N607", start="N607")
    assert already["kind"] == "already_there" and len(already["legs"]) == 1
    print("[ok] build_route shape + text parity + accessible + already-there")


def render_view_test():
    print("\n--- Testing render_view ---")
    import views
    full = views.render_view("5F")
    assert 10000 < len(full) <= 300 * 1024, len(full)
    tile = views.render_view("5F", center=[4065, 1270],
                             width=1600, height=1200)
    assert len(tile) > 1000, len(tile)
    overlay = views.render_view("5F", route=[[4065, 1270], [4475, 950]])
    assert len(overlay) > 1000, len(overlay)
    # Zero-length polyline must not crash the SVG pipeline later.
    empty = views.render_view("5F", route=[])
    assert len(empty) > 1000, len(empty)
    single = views.render_view("5F", route=[[100, 100]])
    assert len(single) > 1000, len(single)
    try:
        views.render_view("9F")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown floor")
    print(f"[ok] render_view sizes + overlays (full-map {len(full)} B)")


# ---------------------------------------------------------------------------
# Real-data wall tests. These assert properties of corridors.json itself and
# of every route built from it, so they keep working unchanged when the wall
# list grows from 8 hand-drawn segments to hundreds of extracted ones.
#
# The assertions are phrased the way a user experiences a route (see
# ROUTE_QUALITY_SPEC.md):
#   * no part of the line goes through a wall            (walkable)
#   * no part of the line scrapes a wall, with only a
#     LOCAL exemption right at the doorway step          (walkable)
#   * the router never quietly charges itself a penalty  (honest)
#   * the line does not wander off absurdly               (efficient)
#
# The authoritative efficiency measure (route length vs the shortest
# WALKABLE path) lives in dev/route_quality.py. This smoke check uses the
# SAME denominator (spec mapping: "far 绕行比 -> 换分母为可走最短路"), with a
# deliberately generous budget. The old euclid denominator is deprecated
# (spec B4): two points on opposite sides of a wall have a tiny straight
# line that nobody can walk, so it flagged perfectly good routes.
# ---------------------------------------------------------------------------
WALL_CLEARANCE_PX = 6        # a route segment must stay this far from any wall
SPINE_MIN_MARGIN_PX = 20     # spine edges must stay this far from any wall
MIN_LEG_STRAIGHT_PX = 100.0  # legs shorter than this are exempt (guards div-by-zero)
# The doorway exemption is LOCAL: only the part of the first/last segment
# within this distance of the endpoint is forgiven. The old test skipped the
# whole first and last segment, which hid every real problem there -- and
# the router's own penalty counter skipped the same segments, so neither
# side of the test noticed.
ENDPOINT_EPS_PX = 12
DETOUR_BUDGET_RATIO = float(os.environ.get("WALL_DETOUR_BUDGET_RATIO", "2.5"))
# Documented residuals, as a cheap smoke bound only. The real efficiency
# budget is dev/route_quality.py L3 (route length / shortest walkable
# path), which is measured against what a person could actually walk.
#
# The sweep iterates place_ids (exact rows, no near=start collapsing), so
# `far` is deduped on (start, place_id, floor, ratio). An earlier version
# deduped on the NAME, which silently merged the two distinct "Phone
# Booth" rows once the sweep started routing by id.
DETOUR_BUDGET_DEFAULT = 6


# --- walkable shortest path (the smoke denominator) -----------------------
# Reuses dev/route_quality.py's FreeSpace raster: an INDEPENDENT
# implementation (it never touches the router's graph), which is the
# "second opinion" the ROUTE_QUALITY_SPEC asks for. Cached per floor and
# per leg start point -- the sweep has only a handful of distinct starts
# (origin anchors + transit landings), so this costs a few Dijkstra runs,
# not one per leg.
_GRID_CACHE = {}
_WALK_CACHE = {}


def _free_grid(floor):
    if floor not in _GRID_CACHE:
        # Loaded by explicit file path (dev/ is a sibling of src/, not an
        # import package) so static analysers need not resolve the name.
        import importlib.util as _ilu
        _path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'dev', 'route_quality.py')
        _spec = _ilu.spec_from_file_location('_route_quality', _path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _GRID_CACHE[floor] = _mod.FreeSpace(floor, library.CORRIDORS[floor])
    return _GRID_CACHE[floor]


def _walkable_length(floor, a, b):
    """Shortest walkable length in px from a to b on `floor`, or None."""
    grid = _free_grid(floor)
    key = (floor, round(a[0]), round(a[1]))
    if key not in _WALK_CACHE:
        _WALK_CACHE[key] = grid.walk_lengths_from(a)
    lengths, _ = _WALK_CACHE[key]
    tgt = grid.snap(*b)
    return lengths.get(tgt) if tgt is not None else None


def _pt_seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_seg_dist(a, b, c, d):
    if library._segments_cross(a, b, c, d):
        return 0.0
    return min(_pt_seg_dist(a, c, d), _pt_seg_dist(b, c, d),
               _pt_seg_dist(c, a, b), _pt_seg_dist(d, a, b))


def _clearance(floor, a, b, eps=0.0):
    """Distance from segment a-b to the nearest wall, ignoring the parts of
    the segment within `eps` of either endpoint.

    The exemption is LOCAL by design: stepping through a doorway means the
    first pixel or two grazes the jamb, and that is fine. Everything beyond
    the doorway has to keep its distance. The previous version of this check
    dropped the whole first/last segment, which let a route scrape along a
    wall for its entire length without anybody noticing.
    """
    walls = library.CORRIDORS[floor].get("walls", [])
    if not walls:
        return float("inf")
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        pts = [(ax, ay)]
    else:
        n = max(2, int(math.sqrt(l2) // 8) + 1)
        pts = [(ax + dx * k / n, ay + dy * k / n) for k in range(n + 1)]
    best = float("inf")
    for px, py in pts:
        if eps > 0:
            if math.hypot(px - ax, py - ay) < eps:
                continue
            if math.hypot(px - bx, py - by) < eps:
                continue
        for w1, w2 in walls:
            d = _pt_seg_dist((px, py), tuple(w1), tuple(w2))
            if d < best:
                best = d
    return best


def _leg_points(leg):
    for key in ("polyline", "points", "path", "pts"):
        if key in leg:
            return [tuple(p) for p in leg[key]]
    raise KeyError(f"leg has no point list (keys: {sorted(leg)}); adjust _leg_points")


def spine_vs_walls_test():
    print("\n--- Testing spine vs walls (data consistency) ---")
    for floor, cfg in library.CORRIDORS.items():
        wps, walls = cfg["waypoints"], cfg.get("walls", [])
        if not walls:
            print(f"[warn] {floor}: no walls loaded, nothing to check")
            continue
        worst = float("inf")
        for a_id, b_id in cfg["edges"]:
            a, b = tuple(wps[a_id]), tuple(wps[b_id])
            for w in walls:
                d = _seg_seg_dist(a, b, tuple(w[0]), tuple(w[1]))
                assert d > 0, f"{floor}: spine edge {a_id}-{b_id} crosses wall {w}"
                worst = min(worst, d)
        assert worst >= SPINE_MIN_MARGIN_PX, f"{floor}: spine only {worst:.0f}px from a wall"
        print(f"[ok] {floor}: {len(walls)} walls, min spine-to-wall margin {worst:.0f}px")


def all_locations_wall_test():
    print("\n--- Testing every location for wall crossings ---")
    crossing_budget = int(os.environ.get("WALL_CROSSING_BUDGET", "0"))
    clearance_budget = int(os.environ.get("WALL_CLEARANCE_BUDGET", "0"))
    rows = library._load_rows()
    # Sweep by stable place_id (not by name): find_location resolves an id
    # to its exact row, so same-name rows (Phone Booth x2, Fire Exit x9,
    # Sound-isolating Earplugs x3) are each covered once instead of
    # collapsing to the instance nearest the start.
    idmap = library._assign_place_ids(rows)
    targets = [(pid, r) for r, pid in zip(rows, idmap)
               if r.get("type") != "Transit"]
    detour_budget = int(os.environ.get("WALL_DETOUR_BUDGET",
                                    str(DETOUR_BUDGET_DEFAULT)))
    crossing, tight, unresolved, far, dirty, total = [], [], [], [], [], 0
    for start in (None, "N607"):
        for pid, row in targets:
            try:
                route = library.build_route(pid, start=start)
            except ValueError:
                unresolved.append((start, pid))
                continue
            total += 1
            for leg in route["legs"]:
                floor, pts = leg["floor"], _leg_points(leg)
                walls = library.CORRIDORS[floor].get("walls", [])
                # Router self-report: zero tolerance. Every leg must route
                # without penalized edges or endpoint/graph fallbacks; this
                # also covers short (<3 point) legs whose inner segment list
                # is empty below.
                diag = leg.get("diag", {})
                if (diag.get("penalty_edges", 0) > 0
                        or diag.get("end_fallback")
                        or diag.get("graph_fallback")):
                    dirty.append((start, pid, floor, dict(diag)))
                # Per-leg detour ratio: a whole-route ratio lets good legs
                # mask bad ones. This is a cheap smoke bound only -- the
                # authoritative measure is dev/route_quality.py L3.
                # Denominator is the shortest WALKABLE path, not the euclid
                # line (spec B4: the euclid denominator is deprecated).
                walk = _walkable_length(floor, pts[0], pts[-1])
                if walk is None:
                    walk = math.hypot(pts[-1][0] - pts[0][0],
                                      pts[-1][1] - pts[0][1])
                if walk >= MIN_LEG_STRAIGHT_PX:
                    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
                    ratio = length / walk
                    if ratio > DETOUR_BUDGET_RATIO:
                        far.append((start, pid, floor, round(ratio, 2)))
                segs = list(zip(pts, pts[1:]))
                # A route that crosses a wall is never walkable, and this
                # holds for EVERY segment including the first and last.
                hit = any(library._segments_cross(a, b, tuple(w[0]), tuple(w[1]))
                          for a, b in segs for w in walls)
                if hit:
                    crossing.append((start, pid, floor))
                # Clearance: every segment is checked, with a local
                # exemption for the pixels right at a doorway step.
                if not hit:
                    for i, (a, b) in enumerate(segs):
                        eps = ENDPOINT_EPS_PX if i in (0, len(segs) - 1) else 0
                        if _clearance(floor, a, b, eps) < WALL_CLEARANCE_PX:
                            tight.append((start, pid, floor, i))
                            break
    # Distinct legs only, so the budget is not spent on duplicate iterations.
    # Keyed by place_id: two rows may share a name (Phone Booth x2) and be
    # genuinely different destinations.
    far = list(dict.fromkeys(far))
    assert not unresolved, f"place_ids did not resolve: {unresolved[:5]}"
    print(f"routes checked: {total}; wall-crossing (penalized fallback): {len(crossing)}; "
          f"under {WALL_CLEARANCE_PX}px clearance: {len(tight)}; "
          f"legs detouring >{DETOUR_BUDGET_RATIO}x: {len(far)}; "
          f"legs with router-reported penalty/fallback: {len(dirty)}")
    assert len(crossing) <= crossing_budget, f"routes crossing walls: {crossing[:10]}"
    assert len(tight) <= clearance_budget, f"routes hugging walls: {tight[:10]}"
    assert not dirty, f"legs paying wall penalty: {dirty[:10]}"
    if far:
        print(f"[warn] {len(far)} documented residual detours: {far}")
    assert len(far) <= detour_budget, f"legs detouring >{DETOUR_BUDGET_RATIO}x: {far[:10]}"
    print("[ok] no route crosses or hugs a wall, and none detours absurdly")


if __name__ == "__main__":
    quick_test()
    directions_test()
    walls_test()
    spine_vs_walls_test()
    all_locations_wall_test()
    facilities_test()
    build_route_test()
    render_view_test()