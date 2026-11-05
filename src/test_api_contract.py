"""Contract tests for the structured route API.

Covers identity preservation, same-name/different-place detection, plan-id
stability, public fallback diagnostics, tool input schemas, and image MIME
truthfulness. Run with:

    PYTHONPATH=src python src/test_api_contract.py
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys

import jsonschema
from PIL import Image

import library
import mcp_server


def _step_ids(plan, kind):
    return [s for leg in plan.legs for s in leg.steps if s.kind == kind]


def test_route_preserves_unique_ids():
    for dest_id, start_id in (
        ("pn-6f-2", None),
        ("phone-booth-6f-2", "phone-booth-6f"),
        ("5f-fire-exit-5f-3", None),
    ):
        plan = library.get_route(dest_id, start=start_id)
        assert plan.destination.id == dest_id, (
            dest_id, plan.destination.id)
        detail = library.get_location_detail(dest_id)
        assert detail.point == plan.destination.point, (
            dest_id, detail.point, plan.destination.point)

        ends = _step_ids(plan, "end")
        assert ends and ends[-1].place_id == dest_id, (
            dest_id, [s.place_id for s in ends])
        if start_id:
            assert plan.origin.id == start_id, (start_id, plan.origin.id)
            starts = _step_ids(plan, "start")
            assert starts and starts[0].place_id == start_id, (
                start_id, [s.place_id for s in starts])
    print("[ok] route/destination/step/detail ids stay aligned")


def test_plan_ids_do_not_collide():
    ids = ("pn-6f", "pn-6f-2", "phone-booth-6f", "phone-booth-6f-2")
    plans = [library.get_route(pid) for pid in ids]
    plan_ids = [p.plan_id for p in plans]
    assert len(set(plan_ids)) == len(plan_ids), plan_ids
    for pid, plan in zip(ids, plans):
        assert library.get_route(pid).plan_id == plan.plan_id, pid
    print("[ok] distinct destinations have distinct, stable plan ids")


def test_same_name_distinct_points_route():
    r = library.build_route("phone-booth-6f-2", start="phone-booth-6f")
    assert r["kind"] == "same_floor", r["kind"]
    assert r["src_id"] != r["dest_id"]
    assert r["src_id"] == "phone-booth-6f"
    assert r["dest_id"] == "phone-booth-6f-2"

    r = library.build_route("phone-booth-6f", start="phone-booth-6f")
    assert r["kind"] == "already_there", r["kind"]
    print("[ok] same-name places route unless they are the same row")


def test_diag_warnings_unit():
    def leg(diag):
        return {"floor": "5F", "diag": diag}

    warnings = library._leg_warnings([
        leg({"penalty_edges": 2, "end_fallback": True,
             "graph_fallback": True, "epsilon_used": True}),
    ])
    text = " | ".join(warnings)
    assert "crosses a wall (2 penalised segment(s))" in text, text
    assert "no visible corridor point" in text, text
    assert "corridor graph is disconnected" in text, text
    assert "grazes a wall within the doorway epsilon" in text, text
    assert library._leg_warnings([leg({})]) == []
    print("[ok] all router diagnostics map to public warnings")


def test_public_warnings_propagate():
    original = library._path_points

    def fake_path_points(floor, start, end, diag):
        diag.update({"penalty_edges": 2, "end_fallback": True,
                     "graph_fallback": True, "epsilon_used": True})
        return [start, end]

    try:
        library._path_points = fake_path_points
        plan = library.get_route("W522")
    finally:
        library._path_points = original

    assert any("crosses a wall" in w for w in plan.warnings), plan.warnings
    assert any("no visible corridor point" in w for w in plan.warnings), \
        plan.warnings
    assert any("corridor graph is disconnected" in w for w in plan.warnings), \
        plan.warnings
    assert any("grazes a wall" in w for w in plan.warnings), plan.warnings
    print("[ok] raw leg diagnostics reach RoutePlan.warnings")


def test_clean_map_has_no_warnings():
    rows = library._load_rows()
    ids = [pid for r, pid in zip(rows, library._assign_place_ids(rows))
           if r["type"] != "Transit"]
    total = 0
    for pid in ids:
        for start in (None, "N607"):
            plan = library.get_route(pid, start=start)
            assert plan.warnings == [], (pid, start, plan.warnings)
            total += 1
    assert total == 220, total
    print(f"[ok] {total} current routes have clean public warnings")


def test_tool_input_schemas():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    detail = tools["get_location_detail"].inputSchema
    assert detail["required"] == ["place_id"], detail
    assert "place_id" in detail["properties"], detail
    assert "id" not in detail["properties"], detail
    jsonschema.validate({"place_id": "n607-6f"}, detail)
    try:
        jsonschema.validate({"id": "n607-6f"}, detail)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("get_location_detail accepted the wrong key")

    view = tools["get_map_view"].inputSchema
    assert view["required"] == ["floor"], view
    jsonschema.validate({
        "floor": "5F", "center": [4065, 1270],
        "width": 1600, "height": 1200, "plan_id": "abc",
    }, view)
    print("[ok] advertised tool schemas match viewer payloads")


def _image_info(result):
    block = next(b for b in result.content
                 if getattr(b, "type", None) == "image")
    raw = base64.b64decode(block.data)
    with Image.open(io.BytesIO(raw)) as img:
        return img.format, block.mimeType


def test_image_mime_matches_bytes():
    expected = "WEBP" if mcp_server.ENABLE_MCP_APPS else "JPEG"
    expected_mime = "image/webp" if expected == "WEBP" else "image/jpeg"
    for result in (
        mcp_server.get_library_map("N607"),
        mcp_server.get_library_directions("W522"),
    ):
        fmt, mime = _image_info(result)
        assert (fmt, mime) == (expected, expected_mime), (fmt, mime)

    fmt, mime = _image_info(mcp_server.get_map_view("5F"))
    assert (fmt, mime) == ("WEBP", "image/webp"), (fmt, mime)
    fmt, mime = _image_info(mcp_server.get_route_3d("N607"))
    assert (fmt, mime) == ("PNG", "image/png"), (fmt, mime)
    print(f"[ok] image MIME matches bytes (apps={'on' if expected == 'WEBP' else 'off'})")


OFF_MIME_PROBE = """
import base64, io, json, sys
sys.path.insert(0, "src")
from PIL import Image
import mcp_server

def info(res):
    block = next(b for b in res.content if getattr(b, "type", None) == "image")
    raw = base64.b64decode(block.data)
    with Image.open(io.BytesIO(raw)) as img:
        return [img.format, block.mimeType]

print(json.dumps({
    "map": info(mcp_server.get_library_map("N607")),
    "directions": info(mcp_server.get_library_directions("W522")),
}))
"""


def test_kill_switch_mime():
    env = dict(os.environ, ENABLE_MCP_APPS="0")
    p = subprocess.run([sys.executable, "-c", OFF_MIME_PROBE], env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    data = json.loads(p.stdout.strip().splitlines()[-1])
    assert data["map"] == ["JPEG", "image/jpeg"], data
    assert data["directions"] == ["JPEG", "image/jpeg"], data
    print("[ok] kill-switch still serves JPEG with the correct MIME")


if __name__ == "__main__":
    test_route_preserves_unique_ids()
    test_plan_ids_do_not_collide()
    test_same_name_distinct_points_route()
    test_diag_warnings_unit()
    test_public_warnings_propagate()
    test_clean_map_has_no_warnings()
    test_tool_input_schemas()
    test_image_mime_matches_bytes()
    test_kill_switch_mime()
    print("All API contract tests passed.")
