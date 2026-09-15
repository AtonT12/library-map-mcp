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


if __name__ == "__main__":
    quick_test()
    directions_test()
    walls_test()
    facilities_test()
