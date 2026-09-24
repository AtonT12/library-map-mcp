"""Step 7 structural tests (plan Step F).

Run: PYTHONPATH=src python src/test_route_schema.py [--write]
--write regenerates schemas/*.json snapshots; default mode asserts the
committed snapshots match the models (schema-drift guard).

Covers: schema snapshots, live-protocol outputSchema + jsonschema
validation, dual-path consistency, search top-1 parity battery,
determinism rings 1-3, boundary cases, visibility, startup/hot-reload.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import library
import models

SCHEMA_DIR = os.path.join(library.BASE_DIR, "schemas")
MODELS = [
    models.Place, models.RouteStep, models.TransitInfo, models.RouteLeg,
    models.RouteTotals, models.RoutePlan, models.LocationMatch,
    models.LocationSearchResult, models.Alert, models.LibraryStatus,
]
LIVE_PORT = 18083


def write_schemas():
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    for m in MODELS:
        path = os.path.join(SCHEMA_DIR, m.__name__ + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m.model_json_schema(), f, indent=2, sort_keys=True)
            f.write("\n")
    print(f"[ok] wrote {len(MODELS)} schemas to schemas/")


def test_schemas_match():
    for m in MODELS:
        path = os.path.join(SCHEMA_DIR, m.__name__ + ".json")
        assert os.path.exists(path), (
            f"{path} missing: run `python src/test_route_schema.py --write`")
        with open(path, encoding="utf-8") as f:
            committed = json.load(f)
        fresh = json.loads(json.dumps(m.model_json_schema(), sort_keys=True))
        assert committed == fresh, (
            f"{m.__name__} schema drifted: re-run with --write and "
            "review the diff before committing")
    print("[ok] schema snapshots match models")


def test_dual_consistency():
    for dest, start in [("N607", None), ("W522", None),
                        ("N509", "W516"), ("QA76", None)]:
        route = library.build_route(dest, start)
        plan = library.get_route(dest, start)
        assert [l.floor for l in plan.legs] == \
            [l["floor"] for l in route["legs"]]
        for leg_p, leg_r in zip(plan.legs, route["legs"]):
            assert [[float(x), float(y)] for x, y in leg_r["points"]] == \
                [list(p) for p in leg_p.polyline], dest
    print("[ok] get_route legs == build_route points (4 cases)")


BATTERY = ["N607", "n607", "IDRL", "Resercher", "QA76.5", "restroom", "WC",
           "restrom", "N509", "The Hub", "water", "print", "N", "printer",
           "W522", "XNOVEXIST"]


def test_top1_parity():
    rows = library._load_rows()
    idmap = library._assign_place_ids(rows)
    key = lambda r: (r.get("name"), r.get("floor"), r.get("x"),
                     r.get("y"), r.get("call_start"))
    id_by_key = {key(r): pid for r, pid in zip(rows, idmap)}
    for q in BATTERY:
        found = library.find_location(q)
        top = library.search_locations(q)
        want = id_by_key[key(found)] if found else None
        got = top.matches[0].place.id if top.matches else None
        assert got == want, f"{q!r}: find={want} search-top1={got}"
    print(f"[ok] search top-1 == find_location ({len(BATTERY)} queries)")


def test_place_ids_unique():
    rows = library._load_rows()
    ids = library._assign_place_ids(rows)
    assert len(set(ids)) == len(rows), "place ids must be globally unique"
    # Formerly shadowed rows (same call_start / same facility name) are
    # now reachable by their suffixed ids.
    by_id = {}
    for r, pid in zip(rows, ids):
        by_id[pid] = r["name"]
    assert by_id["pn-6f"] == "Main Collection (PN)"
    assert by_id["pn-6f-2"] == "Main Collection (PN - Z)"
    assert library.get_location_detail("pn-6f-2").name == \
        "Main Collection (PN - Z)"
    fires = sorted(pid for pid in by_id if pid.startswith("5f-fire-exit"))
    assert fires == ["5f-fire-exit-5f", "5f-fire-exit-5f-2",
                     "5f-fire-exit-5f-3", "5f-fire-exit-5f-4",
                     "5f-fire-exit-5f-5"], fires
    print("[ok] place ids unique; shadowed rows reachable")


def _canon_plan(dest, start=None, accessible_only=False):
    r = library.get_route(dest, start, accessible_only=accessible_only)
    d = r.model_dump()
    d.pop("generated_at")
    return json.dumps(d, sort_keys=True)


def test_determinism():
    a = _canon_plan("N607")
    assert _canon_plan("N607") == a, "ring 1: same ids must repeat plan_id"
    first = library.search_locations("resercher").matches[0].place.id
    for _ in range(20):
        s = library.search_locations("resercher")
        assert s.matches[0].place.id == first
        assert _canon_plan("resercher") == _canon_plan("resercher")
    print("[ok] rings 1+2: plan_id stable, query x20 stable")

    code = (
        "import sys, json; sys.path.insert(0, 'src'); import library;"
        " s = library.search_locations('resercher');"
        " d = library.get_route('resercher').model_dump(); d.pop('generated_at');"
        " print(json.dumps({'top1': s.matches[0].place.id, 'plan': d}, sort_keys=True))")
    outs = []
    for seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        p = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr[-1500:]
        outs.append(p.stdout)
    assert outs[0] == outs[1], "ring 3: PYTHONHASHSEED changed output"
    print("[ok] ring 3: cross-process determinism")


def test_query_boundaries():
    assert library.find_location("洗手间") is None
    assert library.search_locations("洗手间").total_matches == 0
    r = library.find_location("stairs")
    assert r is None or r["type"] != "Transit", r
    assert not library.find_location("x" * 2000)
    s = library.search_locations("restroom")  # documents multi-hit shape
    assert s.total_matches >= 2 and s.exact_matches >= 2, \
        (s.total_matches, s.exact_matches)
    print("[ok] query boundaries (cn/transit/long/multi-hit)")


def test_route_boundaries():
    r = library.get_route("W522", start="N607")  # 6F -> 5F
    assert [l.floor for l in r.legs] == ["6F", "5F"]
    assert r.legs[0].transit.direction == "down"
    r = library.get_route("N607", start="N607")
    assert len(r.legs) == 1 and r.totals.floor_changes == 0
    r = library.get_route("QA76.5")
    assert len(r.legs) >= 1
    r = library.get_route("N607", accessible_only=True)
    assert r.totals.step_free and r.totals.transits_used == ["Elevator"]
    r = library.get_route("N607")
    assert any("6F" in w for w in r.warnings), r.warnings
    for bad in (lambda: library.get_route("nonexistent-xyz"),
                lambda: library.get_route("N607", start="nonexistent")):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    print("[ok] route boundaries (reverse/same/shelf/accessible/warnings/errors)")


def test_missing_map_iserror():
    import mcp_server
    jpg = os.path.join(library.BASE_DIR, "5f_base.jpg")
    tmp = jpg + ".tmp"
    os.rename(jpg, tmp)
    try:
        try:
            library.search_and_draw("W522")
        except Exception as e:
            assert "5f_base.jpg" in str(e), e
        else:
            raise AssertionError("expected loud failure")
        res = mcp_server.get_map_view("5F")
        assert res.isError and "not found" in res.content[0].text, \
            res.content[0].text
    finally:
        os.rename(tmp, jpg)
    assert os.path.exists(jpg)
    print("[ok] missing map -> loud isError with filename")


def test_visibility_flag_on():
    import mcp_server

    async def main():
        tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
        mv = tools["get_map_view"]
        assert mv.meta["ui"]["visibility"] == ["app"], mv.meta
        for n in ("search_locations", "get_route", "get_location_detail",
                  "get_library_status"):
            ui = (tools[n].meta or {}).get("ui", {})
            assert ui.get("resourceUri") == mcp_server.VIEW_URI, (n, ui)
            assert ui.get("visibility", ["model", "app"]) != ["app"], n
            assert tools[n].outputSchema, n
        assert not (tools["get_library_map"].meta or {}).get("ui")
        assert not (tools["get_library_directions"].meta or {}).get("ui")

    asyncio.run(main())
    print("[ok] visibility + outputSchema (flag on)")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def test_startup_and_hotreload():
    cor = os.path.join(library.BASE_DIR, "corridors.json")
    with open(cor, "rb") as f:
        saved = f.read()
    try:
        os.rename(cor, cor + ".tmp")
        p = _run([sys.executable, "src/selfcheck.py"])
        assert p.returncode != 0 and "corridors.json" in p.stderr, p.stderr
        p = _run([sys.executable, "src/mcp_server.py"])
        assert p.returncode != 0, "server must refuse to start"
    finally:
        os.rename(cor + ".tmp", cor)
    p = _run([sys.executable, "src/selfcheck.py"])
    assert p.returncode == 0 and "assets ok" in p.stdout, p.stderr

    before = library._path_points("5F", (3360, 805), (700, 3200))
    with open(cor, "wb") as f:
        f.write(b"{broken")
    during = library._path_points("5F", (3360, 805), (700, 3200))
    assert during == before, "corrupt file must keep serving old data"
    with open(cor, "wb") as f:
        f.write(saved)
    after = library._path_points("5F", (3360, 805), (700, 3200))
    assert after == before
    print("[ok] startup fail-fast + runtime hot-reload keep-old")


def _wait_port(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"server did not listen on {port}")


def test_live_protocol():
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    env = dict(os.environ, PORT=str(LIVE_PORT))
    proc = subprocess.Popen(
        [sys.executable, "src/mcp_server.py"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(LIVE_PORT)

        async def main():
            async with streamablehttp_client(
                    f"http://127.0.0.1:{LIVE_PORT}/mcp") as (read, write, _):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    tools = {t.name: t
                             for t in (await s.list_tools()).tools}
                    for n in ("search_locations", "get_route",
                              "get_location_detail", "get_library_status"):
                        assert tools[n].outputSchema, n
                    assert tools["get_map_view"].meta["ui"][
                        "visibility"] == ["app"]
                    r = await s.call_tool("get_route",
                                          {"destination": "N607"})
                    assert not r.isError
                    assert r.structuredContent, "no structuredContent"
                    try:
                        import jsonschema
                        path = os.path.join(
                            SCHEMA_DIR, "RoutePlan.json")
                        with open(path, encoding="utf-8") as f:
                            schema = json.load(f)
                        jsonschema.validate(r.structuredContent, schema)
                    except ImportError:  # pragma: no cover
                        TypeAdapter = __import__(
                            "pydantic", fromlist=["TypeAdapter"]).TypeAdapter
                        TypeAdapter(models.RoutePlan).validate_python(
                            r.structuredContent)
                    assert len(json.dumps(r.structuredContent)) <= 20 * 1024
                    res = await s.list_resources()
                    assert "ui://nav/library-view.html" in \
                        [str(x.uri) for x in res.resources]
                    rr = await s.read_resource("ui://nav/library-view.html")
                    assert rr.contents[0].mimeType == \
                        "text/html;profile=mcp-app"

        asyncio.run(main())
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    print("[ok] live protocol (schemas validate, resource readable)")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_schemas()
    else:
        test_schemas_match()
        test_dual_consistency()
        test_top1_parity()
        test_place_ids_unique()
        test_determinism()
        test_query_boundaries()
        test_route_boundaries()
        test_missing_map_iserror()
        test_visibility_flag_on()
        test_startup_and_hotreload()
        test_live_protocol()
        print("All route-schema tests passed.")
