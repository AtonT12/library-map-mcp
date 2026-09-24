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
- **Architecture**: **MCP server** (FastMCP, Streamable HTTP): 4 structured
  tools (`search_locations`, `get_route`, `get_location_detail`,
  `get_library_status`), 1 app-only tile tool (`get_map_view`), and 2
  legacy image tools kept as fallback.
- **Maintenance**: Includes web-based Calibration (`calibrate.html`) and
  Corridor (`corridors.html`) editors, plus a startup asset self-check.

## Roadmap
- [x] Integration with official Chatbot via MCP Server (server side done;
  host-side verification on Ask pending).
- [x] Mobile-friendly maps: WebP output, 1600 px cap, zoom tiles, prebuilt
  thumbnails (`scripts/build_static.py`).
- [ ] Real-time emergency status updates: `get_library_status` reads the
  optional `status.json` (placeholder); needs a real maintainer/feed.
