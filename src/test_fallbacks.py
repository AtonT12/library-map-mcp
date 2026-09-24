"""Step 6 fallback invariants + 5-2 registration probe (plan Step E).

Server-side only: no browser, no ports. Run with
`PYTHONPATH=src python src/test_fallbacks.py`.

L1: with ENABLE_MCP_APPS=0 there are no ui:// resources, no _meta.ui on
    any tool, and the old image tools return the frozen bytes.
L4: unknown ids/floors fail loudly (ValueError / isError), never silently.
5-2 probe: 6F-vs-5F transit identity residual must stay < 100 px --
    guards against re-cropped base maps or width-normalized scaling.
"""

import asyncio
import math
import os
import subprocess
import sys

import library
import views
import mcp_server

FROZEN = {
    "map_Resercher": 1483239,
    "same_W522": 1661805,
    "cross_N607": [1627310, 1505030],
    "cross_QA76": [1627310, 1505390],
}


L1_PROBE = """
import asyncio, sys
sys.path.insert(0, "src")
import library, mcp_server

assert not library.ENABLE_MCP_APPS and not mcp_server.ENABLE_MCP_APPS

async def main():
    tools = await mcp_server.mcp.list_tools()
    res = await mcp_server.mcp.list_resources()
    assert res == [], res
    bad = [(t.name, t.meta) for t in tools if (t.meta or {}).get("ui")]
    assert not bad, bad
    _, img = library.search_and_draw("Resercher")
    assert len(img) == %(map)d, len(img)
    _, imgs = library.get_directions("W522")
    assert len(imgs[0][1]) == %(same)d
    _, imgs = library.get_directions("N607")
    assert [len(b) for _, b in imgs] == %(n607)s
    _, imgs = library.get_directions("QA76")
    assert [len(b) for _, b in imgs] == %(qa76)s
    print("L1 ok:", sorted(t.name for t in tools))

asyncio.run(main())
""" % {"map": FROZEN["map_Resercher"], "same": FROZEN["same_W522"],
       "n607": FROZEN["cross_N607"], "qa76": FROZEN["cross_QA76"]}


def test_l1_kill_switch():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py",
                                     delete=False) as f:
        f.write(L1_PROBE)
        probe = f.name
    env = dict(os.environ, ENABLE_MCP_APPS="0")
    p = subprocess.run([sys.executable, probe], env=env,
                       capture_output=True, text=True)
    print(p.stdout, end="")
    assert p.returncode == 0, p.stderr[-2000:]
    print("[ok] L1 kill-switch: no resources, no _meta.ui, bytes frozen")


def test_l4_errors():
    for fn in (lambda: library.get_route("nonexistent-xyz"),
               lambda: library.get_location_detail("nope"),
               lambda: views.render_view("9F")):
        try:
            fn()
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError("expected loud failure")
    res = mcp_server.get_map_view("9F")
    assert res.isError, "bad floor must be isError"
    # Unknown ids must point the way out, not just say no.
    try:
        library.get_location_detail("N607")
    except ValueError as e:
        assert "n607-6f" in str(e) and "search_locations" in str(e), e
    else:
        raise AssertionError("expected guided failure")
    print("[ok] L4 error paths fail loudly")


def test_transit_registration():
    pts = {}
    for r in library._load_rows():
        if r["type"] == "Transit" and r.get("call_start"):
            pts.setdefault(r["call_start"], {})[r["floor"]] = (
                int(r["x"]), int(r["y"]))
    worst = 0.0
    for cid, floors in sorted(pts.items()):
        a, b = floors["5F"], floors["6F"]
        d = math.hypot(a[0] - b[0], a[1] - b[1])
        print(f"  {cid}: residual {d:.1f} px")
        worst = max(worst, d)
    assert worst < 100, f"registration drift: {worst}"
    print(f"[ok] registration probe: worst residual {worst:.1f} px < 100")


if __name__ == "__main__":
    test_l1_kill_switch()
    test_l4_errors()
    test_transit_registration()
    print("All fallback tests passed.")
