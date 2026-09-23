"""Slim map renderer (plan Step 4): ROI crop + downscale + WebP.

render_view() is the single image pipeline behind get_map_view and -- when
ENABLE_MCP_APPS=1 -- behind search_and_draw()/_draw_route(). With the flag
off those keep their original JPEG bytes untouched (Step 6/L3 rollback).
"""

import io
import os
from PIL import Image, ImageDraw

import library

LONG_SIDE_MAX = 1600


def floor_for_map(map_file):
    """Floor whose corridors.json entry uses `map_file`, else None."""
    base = os.path.basename(map_file)
    for floor, info in library.CORRIDORS.items():
        if os.path.basename(info.get('map_file', '')) == base:
            return floor
    return None


def render_view(floor, center=None, width=1280, height=960, scale=1.0,
                plan_id=None, show_labels=True, route=None,
                start_color="green", end_color="red", pin=None,
                fmt="webp", quality=82):
    """Render a floor-map view, return encoded bytes.

    floor: "5F"/"6F". center: full-res (x, y) ROI center, or None for the
    whole map. width/height: ROI box size in full-res px (clamped to the
    image). scale: extra downscale; the result's long side is additionally
    capped at 1600 px. route: full-res polyline drawn with the _draw_route
    casing/core geometry; pin: single full-res marker (search pin).
    plan_id is accepted and reserved for Step 5 tile-cache correlation.
    """
    _ = plan_id
    if floor not in library.CORRIDORS:
        raise ValueError(f"Unknown floor '{floor}'.")
    path = os.path.join(library.BASE_DIR,
                        library.CORRIDORS[floor]['map_file'])
    if not os.path.exists(path):
        raise FileNotFoundError(f"Base map {path} not found.")
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    full_w, full_h = img.size

    ox, oy = 0, 0
    if center is not None:
        cx, cy = float(center[0]), float(center[1])
        bw, bh = min(int(width), full_w), min(int(height), full_h)
        ox = int(min(max(cx - bw / 2, 0), full_w - bw))
        oy = int(min(max(cy - bh / 2, 0), full_h - bh))
        img = img.crop((ox, oy, ox + bw, oy + bh))

    k = float(scale)
    if max(img.width, img.height) * k > LONG_SIDE_MAX:
        k = LONG_SIDE_MAX / max(img.width, img.height)
    if k != 1.0:
        img = img.resize((max(1, int(img.width * k)),
                          max(1, int(img.height * k))), Image.LANCZOS)

    def tx(x, y):
        return ((x - ox) * k, (y - oy) * k)

    draw = ImageDraw.Draw(img)

    def marker(px, py, fill):
        r = max(2, int(round(60 * k)))
        draw.ellipse((px - r, py - r, px + r, py + r),
                     fill=fill, outline="white", width=max(1, int(round(15 * k))))

    if route:
        pts = [tx(x, y) for x, y in route]
        if len(pts) >= 2:
            lw = max(1, int(round(34 * k)))
            cw = max(1, int(round(18 * k)))
            draw.line(pts, fill="white", width=lw, joint="curve")
            draw.line(pts, fill="#1E6FFF", width=cw, joint="curve")
        sx, sy = pts[0]
        ex, ey = pts[-1]
        marker(sx, sy, start_color)
        marker(ex, ey, end_color)
        cr = max(2, int(round(15 * k)))
        draw.ellipse((ex - cr, ey - cr, ex + cr, ey + cr), fill="white")
        if show_labels:
            draw.text((sx + 60 * k + 4, sy - 60 * k), "S", fill="green")
            draw.text((ex + 60 * k + 4, ey - 60 * k), "E", fill="red")

    if pin is not None:
        px, py = tx(pin[0], pin[1])
        marker(px, py, "red")
        cr = max(2, int(round(15 * k)))
        draw.ellipse((px - cr, py - cr, px + cr, py + cr), fill="white")

    buf = io.BytesIO()
    if fmt == "webp":
        img.save(buf, format="WEBP", quality=quality)
    elif fmt == "jpeg":
        img.save(buf, format="JPEG", quality=quality)
    else:
        raise ValueError(f"Unsupported format '{fmt}'.")
    return buf.getvalue()
