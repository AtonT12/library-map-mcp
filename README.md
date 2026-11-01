# NYUSH Library Visual Navigator

A specialized visual grounding tool designed to enhance the NYU Shanghai Library AI Chatbot.

## The Problem
Currently, the library chatbot provides text-based answers. However, patrons often struggle to find physical locations (rooms and bookshelves) using text alone.

## The Solution
This project bridges the gap by:
- Mapping **Room Names** (e.g., N607) to floor plan coordinates.
- Using a range-based algorithm to locate **Library of Congress Call Numbers** (e.g., QA76).
- Returning **structured route data** (per-floor polylines + steps JSON) plus a
  **slim WebP map** with the route drawn, instead of one heavy JPEG.
- Rendering an **interactive map view** (pan/zoom, floor tabs, oblique 3D,
  tap-a-station details) via MCP Apps (`ui://nav/library-view.html`).

## Tech Stack
- **Language**: Python (Pillow for image processing, pydantic for tool schemas)
- **Architecture**: **MCP server** (FastMCP, Streamable HTTP) exposing 8 tools:
  4 structured (`search_locations`, `get_route`, `get_location_detail`,
  `get_library_status`), 1 app-only tile tool (`get_map_view`), 1 static 3D
  image tool (`get_route_3d`), and 2 legacy image tools (`get_library_map`,
  `get_library_directions`) kept as the pre-App fallback.
- **View**: one self-contained HTML app (`ui://nav/library-view.html`) built by
  `src/view/template.py` + `viewer.js` — 2D pan/zoom and oblique 3D, no
  external requests (CSP `resourceDomains: []`).
- **Maintenance**: Includes two browser editors — `corridors.html` for walls
  and the walkable spine (the single source of truth, exported to
  `corridors.json`) and `calibrate.html` for marking location points on the
  base map — plus a startup asset self-check.

## Running locally

```bash
pip install -r requirements.txt
python scripts/build_static.py   # required: src/static/ is a gitignored build artifact
python src/mcp_server.py         # Streamable HTTP on 0.0.0.0:8000/mcp
```

`python src/selfcheck.py` prints `assets ok` (or the failing asset list); the
server runs the same check on startup and exits 1 on failure, so a passing
startup prints nothing about assets.

`ENABLE_MCP_APPS=0` is the rollback switch, not a tuning knob: no `ui://`
resource, no `_meta.ui`, and the legacy image tools return the previous
full-resolution JPEGs byte-for-byte.

## Tests and quality gates

Run from the repo root. Every Python command needs `src/` on `PYTHONPATH`:

```bash
python scripts/build_static.py              # once: the view and schema tests need the WebP thumbnails
PYTHONPATH=src python src/test.py           # 220 routes: no crossing, no <6px clearance, no paid penalty
PYTHONPATH=src python src/test_fallbacks.py # kill-switch invariants + frozen legacy image bytes
PYTHONPATH=src python src/test_route_schema.py  # models vs schemas/*.json snapshots + live protocol
PYTHONPATH=src python dev/route_quality.py  # L1 reachable / L2 walkable / L3 efficient / L4 markers
PYTHONPATH=src python dev/xcheck_raster.py  # independent raster check; any disagreement fails
node dev/viewer.test.js                     # view logic on a fake DOM (CI uses Node 20)
docker build .                              # startup self-check + static pregen
```

`test.py` and `xcheck_raster.py` are deliberately separate implementations: a
test that shares its geometry with the code under test cannot catch a shared
mistake.

## Roadmap
- [x] Integration with official Chatbot via MCP Server — server side done and
  exercised end to end in a local Ask stack; host behaviour is verified by hand
  (`docs/RUNBOOK_ask-local-dev.md`), not in CI.
- [x] Mobile-friendly maps: WebP output, 1600 px cap, zoom tiles, prebuilt
  thumbnails (`scripts/build_static.py`).
- [x] Interactive 3D route view (`ui://nav/library-view.html`) plus a
  server-side 3D PNG (`get_route_3d`) for hosts without iframe support.
- [ ] Real-time emergency status updates: `get_library_status` reads the
  optional `status.json` (placeholder); needs a real maintainer/feed.
