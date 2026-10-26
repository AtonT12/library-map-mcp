import base64
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent
from models import LibraryStatus, LocationSearchResult, Place, RoutePlan

import library
import render_3d
import selfcheck
import views
from view import template

# Kill-switch for the MCP App view (plan Step 6/L1): when "0", no ui://
# resource is registered and no _meta.ui is attached. Tool behavior is
# otherwise unchanged. Consumed by the new tools from Step 3 on.
ENABLE_MCP_APPS = os.environ.get("ENABLE_MCP_APPS", "1") == "1"

mcp = FastMCP(
    "NYUSH_Library_Navigator",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)


@mcp.tool()
def get_library_map(query: str) -> CallToolResult:
    """Show WHERE a single library location is by pinning it on the floor map.

    Use this when the user asks where something is and does NOT ask how to get
    there -- e.g. "Where is room N607?", "Which floor is the Scholars Space on?",
    "Find call number QA76.5", "Where are the PN books?", "Is there a restroom
    / water dispenser / printer?". For step-by-step
    walking directions between two places, use get_library_directions instead.

    Args:
        query: A room name or number (e.g. 'N607', 'The Hub', 'IDRL'), a call
            number (e.g. 'QA76.5'), a shelf range, or a facility (e.g.
            'restroom', 'WC', 'water dispenser', 'printer', 'book drop',
            'Bloomberg Terminal'). Acronyms and minor typos are tolerated.

    Returns:
        A short text description plus one annotated floor-map image (JPEG) with
        a red marker on the location. For a facility found on both floors the
        text says which other floor also has one. Always show the returned
        image to the user.
    """
    try:
        result_msg, image_bytes = library.search_and_draw(query)
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Query failed: {e}")],
            isError=True,
        )

    return CallToolResult(
        content=[
            TextContent(type="text", text=result_msg),
            ImageContent(
                type="image",
                data=base64.b64encode(image_bytes).decode("ascii"),
                mimeType="image/jpeg",
            ),
        ],
    )


@mcp.tool()
def get_library_directions(destination: str, start: str = None) -> CallToolResult:
    """Show HOW TO WALK to a place by drawing the route on the floor map.

    Use this whenever the user wants to get somewhere -- e.g. "How do I get to
    N607?", "Directions to the Scholars Space", "Take me from The Hub to the XR
    Space", "Where's QA76 and how do I walk there?", "Where's the nearest
    restroom?". For simply showing where a
    place is (a single pin, no route), use get_library_map instead.

    This returns FLAT 2D floor map IMAGES. For the INTERACTIVE route view
    -- an embedded 3D model of the building with the route running through
    it, which the user can pan, zoom and click -- call get_route instead.
    Prefer get_route for a plain "how do I get to X" question. Use this
    tool only when the user explicitly wants flat 2D map images, or when
    they ask for a 3D / three-dimensional view, in which case call
    get_route_3d for an extruded 3D model image.

    The library occupies only the 5th floor (5F) and 6th floor (6F); the
    Entrance & Exit is ON 5F (there is no separate ground floor). Do NOT add a
    floor-change step on your own -- rely on the returned text: if it says the
    trip stays on one floor, there are no stairs or elevator; only a 5F<->6F
    trip involves a vertical connection.

    Args:
        destination: Where the user wants to go -- a room name/number, call
            number, shelf, or facility (e.g. 'N607', 'XR Space', 'QA76.5',
            'restroom', 'water dispenser', 'printer'). For a facility that
            exists on several floors, the instance nearest the start is chosen
            automatically (prefer passing just 'restroom', not a floor).
            Acronyms and minor typos are tolerated.
        start: Where the user is starting FROM. Pass this whenever the user
            states their current location (e.g. "I'm at the entrance", "from
            N509"). If omitted, the route begins at the library Entrance & Exit.

    Returns:
        Text directions plus annotated floor-map image(s) (JPEG) showing the
        route -- green marker = start, red marker = destination. Routes follow
        the corridors and avoid walls. A trip that
        stays on one floor returns ONE map. A trip between 5F and 6F returns TWO
        maps (one per floor); the text names the EXACT vertical connection to
        use (e.g. "Central Stairs", "West Stairs", or "Elevator"), chosen by
        proximity. When relaying directions, name only that specific connection
        -- do NOT say a vague "stairs/elevator" or invent a different one. Repeat
        the floor names exactly as given and present every image, in order.
    """
    try:
        result_msg, images = library.get_directions(destination, start)
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Directions failed: {e}")],
            isError=True,
        )

    content = [TextContent(type="text", text=result_msg)]
    for _floor, image_bytes in images:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image_bytes).decode("ascii"),
                mimeType="image/jpeg",
            )
        )
    return CallToolResult(content=content)


VIEW_URI = "ui://nav/library-view.html"


if ENABLE_MCP_APPS:
    @mcp.resource(VIEW_URI, name="library_view",
                  mime_type="text/html;profile=mcp-app",
                  meta={"ui": {"csp": {"resourceDomains": [],
                                       "connectDomains": []}}})
    def library_view() -> str:
        """Interactive route view (single self-contained HTML)."""
        return template.build_view_html()


def _ui_meta(visibility=None):
    """_meta for MCP App tools. None when the kill-switch is off (Step 6/L1):
    the tool then registers as a plain structured tool with no UI binding."""
    if not ENABLE_MCP_APPS:
        return None
    ui = {"resourceUri": VIEW_URI}
    if visibility is not None:
        ui["visibility"] = visibility
    return {"ui": ui}


@mcp.tool(title="Show a 3D model of the route")
def get_route_3d(destination: str, start: str = None,
                 accessible_only: bool = False) -> CallToolResult:
    """Render the walking route as a 3D MODEL of the library building.

    Use this when the user asks for a 3D / three-dimensional / stereoscopic
    view of the route, or simply wants to SEE the route rather than read it
    -- e.g. "Show me a 3D map of the way to N607", "I want to see the
    building", "Can you render this in 3D?". For a flat 2D floor map use
    get_library_directions; for the interactive, clickable view use
    get_route.

    The image shows both floors as extruded floor plates, the real walls
    standing up as translucent faces, and the route floating above the wall
    tops with the stair/elevator link drawn between the two levels.

    Args:
        destination: Where the user wants to go -- a room name/number, call
            number, shelf, or facility (e.g. 'N607', 'XR Space', 'QA76.5',
            'restroom'). Acronyms and minor typos are tolerated.
        start: Where the user starts FROM. If omitted, the route begins at
            the library Entrance & Exit (on 5F).
        accessible_only: When true, floor changes use the elevator only.

    Returns:
        A short text summary plus one 3D model image (PNG) of the route.
        Always show the returned image to the user.
    """
    try:
        plan = library.get_route(destination, start,
                                 accessible_only=accessible_only)
        png = render_3d.render_3d(plan.model_dump())
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"3D render failed: {e}")],
            isError=True,
        )
    summary = (
        "3D model of the route from %s to %s (%s -> %s)."
        % (plan.origin.name, plan.destination.name,
           plan.origin.floor, plan.destination.floor)
    )
    return CallToolResult(
        content=[
            TextContent(type="text", text=summary),
            ImageContent(
                type="image",
                data=base64.b64encode(png).decode("ascii"),
                mimeType="image/png",
            ),
        ],
    )


@mcp.tool(title="Search library locations", meta=_ui_meta())
def search_locations(query: str, limit: int = 5) -> LocationSearchResult:
    """Search library locations by name, acronym, or call number.

    Returns a ranked candidate list with stable place ids -- pass an id to
    get_location_detail or get_route. Use this when a query may match
    several places. Acronyms and minor typos are tolerated.

    Args:
        query: A room name or number (e.g. 'N607', 'The Hub'), a call
            number (e.g. 'QA76.5'), or a facility (e.g. 'restroom').
        limit: Maximum candidates to return.
    """
    try:
        return library.search_locations(query, limit=limit)
    except Exception as e:
        raise ValueError(f"Search failed: {e}")


@mcp.tool(title="Plan a walking route", meta=_ui_meta())
def get_route(destination: str, start: str = None,
              accessible_only: bool = False,
              view_mode: str = "3d") -> RoutePlan:
    """Plan a walking route between two library locations as structured data.

    This is the PREFERRED tool for "how do I get to X" questions. It returns
    per-floor polylines in full-res pixel coords plus human steps, and the
    interactive view turns that into an embedded 3D MODEL of the building:
    both floors as extruded plates, the real walls standing up, and the
    route running through it. The user can pan, zoom, switch floors and
    click any place for details.

    Use get_library_directions instead only when the user explicitly wants
    flat 2D map images, and get_route_3d when they want a 3D model IMAGE
    rather than the interactive view.

    Args:
        destination: A place id from search_locations, or a room
            name/call number.
        start: A place id or name. If omitted, the route begins at the
            library Entrance & Exit.
        accessible_only: When true, floor changes use the elevator only.
        view_mode: "3d" (default) for the extruded 3D model, "2d" for a
            flat floor plan. Use "2d" only when the user asks for a plain
            2D map.
    """
    try:
        return library.get_route(destination, start,
                                 accessible_only=accessible_only,
                                 view_mode=view_mode)
    except Exception as e:
        raise ValueError(f"Route failed: {e}")


@mcp.tool(title="Show a library place", meta=_ui_meta())
def get_location_detail(place_id: str) -> Place:
    """Full record for one place id from search_locations: name, floor,
    coordinates, call number, and which other floors share the facility.

    Args:
        place_id: A place id from search_locations (e.g. 'n607-6f').
    """
    try:
        return library.get_location_detail(place_id)
    except Exception as e:
        raise ValueError(f"Detail failed: {e}")


@mcp.tool(title="Show library status", meta=_ui_meta())
def get_library_status() -> LibraryStatus:
    """Current closure/status layer (floor closures, maintenance).

    Reads the optional status.json; when absent, reports source "none"
    with an empty closure list.
    """
    try:
        return library.get_library_status()
    except Exception as e:
        raise ValueError(f"Status failed: {e}")


@mcp.tool(title="Render a floor-map view", meta=_ui_meta(visibility=["app"]))
def get_map_view(floor: str, center: Optional[list] = None,
                 width: int = 1600, height: int = 1200,
                 plan_id: str = None, route: Optional[list] = None) -> CallToolResult:
    """Render a cropped, high-resolution floor-map tile as WebP.

    Internal to the interactive route view: it is called by the view when
    the user zooms in, to fetch a cropped high-resolution tile.

    Do NOT call this tool directly. To show a map pin use get_library_map;
    to show walking directions use get_library_directions.

    Args:
        floor: "5F" or "6F".
        center: Full-res [x, y] ROI center. If omitted, the whole map
            (downscaled to a 1600 px long side) is returned.
        width: ROI box width in full-res px.
        height: ROI box height in full-res px.
        plan_id: Route plan this tile belongs to (cache correlation).
        route: Optional full-res polyline [[x, y], ...] overlaid on the
            tile (used by the in-view degraded fallback).
    """
    try:
        image_bytes = views.render_view(floor, center=center, width=width,
                                        height=height, plan_id=plan_id,
                                        route=route)
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Map view failed: {e}")],
            isError=True,
        )
    where = f"center {center}, {width}x{height}px ROI" if center else "full map"
    return CallToolResult(
        content=[
            TextContent(type="text",
                        text=f"Floor-map view for {floor} ({where})."),
            ImageContent(
                type="image",
                data=base64.b64encode(image_bytes).decode("ascii"),
                mimeType="image/webp",
            ),
        ],
    )


if __name__ == "__main__":
    asset_problems = selfcheck.verify_assets()
    if asset_problems:
        print("asset check failed, refusing to start:", file=sys.stderr)
        for problem in asset_problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="streamable-http")
