"""Server-side 3D model renderer.

Renders the same extruded model the interactive view draws, but as a static
PNG that a model can show directly in the chat. This is the fallback for
hosts that do not render MCP Apps, and it is the picture that appears when
someone asks for a 3D view of the route.

Geometry mirrors src/view/viewer.js:
    project(x, y, z) = [S*(x + SH*y), S*SQ*y - z]
The base map is affine-transformed into the oblique plane, the walls from
corridors.json are extruded into vertical faces, and the route is drawn
floating above the wall tops.

Only ASCII labels are drawn: the container ships no fonts, so a CJK label
would render as empty boxes. The floor name is the only text.
"""
import io
import math
import os

from PIL import Image, ImageDraw, ImageFilter

import library
from view.plan_outline import PLAN_OUTLINE

# Oblique cabinet projection parameters (same defaults as the view).
SQ = 0.62
SH = (1 - SQ) * 0.18

WALL_RGB = (141, 123, 158)
WALL_EDGE = (74, 63, 86)
ROUTE_RGB = (30, 111, 255)
BG = (32, 36, 42)

# Floor separation as a fraction of the projected floor height.
Z6_RATIO = 0.34

# Rendered size of the final PNG.
OUT_W = 1600


def _font(size):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        for root in ("/usr/share/fonts/truetype/dejavu",
                     "/usr/share/fonts/dejavu",
                     "C:\\Windows\\Fonts"):
            try:
                return ImageFont.truetype(os.path.join(root, name), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _project(x, y, z, S):
    return (S * (x + SH * y), S * SQ * y - z)


def _outline(floor):
    return PLAN_OUTLINE.get(floor) or []


def _render_affine(crop, rect, z, S, out_size, offset):
    """Map a crop of the base map onto its projected quad.

    The oblique projection turns an axis-aligned map rectangle into a
    general quadrilateral, so this uses Pillow's QUAD transform (four
    source corners -> four destination corners) instead of an affine map.

    The crop is expected in the same pixel space as `rect` (the caller
    crops the downsampled map and scales the rectangle to match), so the
    destination quad can be computed directly from the rectangle.

    Pillow's QUAD fills the whole output canvas -- the pixels outside the
    quad are copied, not transparent -- so the result is masked back to the
    quad afterwards. Without that mask every floor would paint over the
    others.
    """
    x0, y0, x1, y1 = rect
    # Upper-left, lower-left, lower-right, upper-right (Pillow's order).
    quad = [_project(x0, y0, z, S), _project(x0, y1, z, S),
            _project(x1, y1, z, S), _project(x1, y0, z, S)]
    dst = [(p[0] + offset[0], p[1] + offset[1]) for p in quad]
    src = [(0, 0), (0, crop.height), (crop.width, crop.height), (crop.width, 0)]
    data = []
    for p in src:
        data.extend(p)
    for p in dst:
        data.extend(p)
    warped = crop.transform(out_size, Image.QUAD, data, resample=Image.BILINEAR)
    mask = Image.new("L", out_size, 0)
    ImageDraw.Draw(mask).polygon(dst, fill=255)
    warped.putalpha(mask)
    return warped


def _small_map(floor):
    """Downsampled base map + the scale of the plan outline into it.

    src/static holds 1500-ish px copies of the base maps (built by
    scripts/build_static.py for the interactive view). They are small enough
    to fit in the container's memory budget, unlike the 120 MB full-res
    maps. The returned outline is the plan outline rescaled into that
    image's pixel space, so the projection works in one consistent space.
    """
    static = os.path.join(library.BASE_DIR, "src", "static")
    name = "5f_small.webp" if floor == "5F" else "6f_small.webp"
    path = os.path.join(static, name)
    if not os.path.exists(path):
        return None
    im = Image.open(path).convert("RGBA")
    m = library.CORRIDORS[floor]["map_file"]
    with Image.open(library.map_path(m)) as full:
        fw, fh = full.size
    kx, ky = im.width / float(fw), im.height / float(fh)
    rects = [(r[0] * kx, r[1] * ky, r[2] * kx, r[3] * ky)
             for r in _outline(floor)]
    return im, rects, kx


def render_3d(plan, width=OUT_W):
    """Render a RoutePlan (or route dict) as a static 3D model PNG."""
    height = int(width * 9 / 16)
    legs = plan["legs"] if isinstance(plan, dict) else [
        l.model_dump() for l in plan.legs]
    if not legs:
        raise ValueError("no legs to render")

    floors = []
    for l in legs:
        if l["floor"] not in floors:
            floors.append(l["floor"])

    # Everything below works in the DOWNSAMPLED image space (src/static,
    # ~1500 px wide). The full-res maps are 120 MB each and blow the
    # container's 256 MB budget; the small copies are 6 MB. The plan
    # outline, the wall segments and the route are all rescaled into that
    # space, so the projection has a single consistent coordinate system.
    maps = {}
    scale_of = {}
    for f in floors:
        m = _small_map(f)
        if m is None:
            raise ValueError("no small map for %s" % f)
        maps[f] = (m[0], m[1])
        scale_of[f] = m[2]

    def outline(f):
        """Plan outline of a floor in the small-map pixel space."""
        return maps[f][1]

    def k_of(f):
        return scale_of[f]

    def to_small(f, x, y):
        k = k_of(f)
        return (x * k, y * k)

    # Fit the scale in one step instead of iterating: the floor separation
    # depends on the scale and the scale depends on the separation, so a
    # naive fixed-point iteration oscillates between two values.
    #
    # Projected extents (for a rectangle of size w x h):
    #     dx = S*(w + SH*h)          dy = S*SQ*h
    # The 6F plate is lifted by z6 = Z6_RATIO * UH * S, so the total height
    # is S*(SQ*UH + Z6_RATIO*UH) and the total width S*(UW + SH*UH).
    UW = max(max(r[2] for r in outline(f)) for f in floors)
    UH = max(max(r[3] for r in outline(f)) for f in floors)
    span_x = UW + SH * UH
    span_y = SQ * UH + Z6_RATIO * UH
    S = min(width * 0.96 / max(1.0, span_x),
            height * 0.96 / max(1.0, span_y))
    z6 = Z6_RATIO * UH * S

    pts = []
    for f in floors:
        z = z6 if f == "6F" else 0
        for r in outline(f):
            for (x, y) in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])):
                p = _project(x, y, z, S)
                # The wall extrusions rise above the plate and the slab has
                # thickness below it: both must fit in the frame.
                pts.append((p[0], p[1] - 0.030 * UH * S))
                pts.append((p[0], p[1] + 0.010 * UH * S))
    minx = min(p[0] for p in pts)
    maxx = max(p[0] for p in pts)
    miny = min(p[1] for p in pts)
    maxy = max(p[1] for p in pts)
    # Centre the model in the canvas instead of pinning it to the top-left.
    off = ((width - (maxx - minx)) / 2 - minx,
           (height - (maxy - miny)) / 2 - miny)
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas, "RGBA")

    thick = 0.010 * UH * S
    wall_h = 0.030 * UH * S
    lift = 0.018 * UH * S + wall_h * 1.15

    floors_asc = sorted(floors, key=lambda f: 0 if f == "5F" else 1)
    for f in floors_asc:
        z = z6 if f == "6F" else 0
        small_im, rects = maps[f]
        k = k_of(f)
        if not rects:
            continue
        # Slab: soft shadow, extruded sides, top face.
        for r in rects:
            quad = [_project(r[0], r[1], z, S), _project(r[2], r[1], z, S),
                    _project(r[2], r[3], z, S), _project(r[0], r[3], z, S)]
            quad = [(p[0] + off[0], p[1] + off[1]) for p in quad]
            shadow = [(p[0] + 12, p[1] + 14) for p in quad]
            draw.polygon(shadow, fill=(0, 0, 0, 60))
            draw.polygon([quad[1], quad[2],
                          (quad[2][0], quad[2][1] + thick),
                          (quad[1][0], quad[1][1] + thick)],
                         fill=(143, 153, 163, 235))
            draw.polygon(quad, fill=(232, 236, 240, 255))
        # Base map texture, warped into each rectangle. Only the plan
        # rectangle is used -- the rest of the map is the legend band, which
        # must not end up on the floor plate.
        for r in rects:
            crop = small_im.crop((int(r[0]), int(r[1]),
                                  int(r[2]), int(r[3])))
            warped = _render_affine(crop, r, z, S, (width, height), off)
            if warped is not None:
                canvas.paste(warped, (0, 0), warped)
        # Extruded walls, far to near. They are the 3D cue, so they must read
        # against the floor plan underneath: a warm tone plus a bright top
        # edge, both clearly different from the drawing's own wall ink.
        walls = library.CORRIDORS[f].get("walls", [])
        for seg in sorted(walls, key=lambda s: s[0][1] + s[1][1]):
            p0 = to_small(f, seg[0][0], seg[0][1])
            p1 = to_small(f, seg[1][0], seg[1][1])
            a = _project(p0[0], p0[1], z, S)
            b = _project(p1[0], p1[1], z, S)
            a = (a[0] + off[0], a[1] + off[1])
            b = (b[0] + off[0], b[1] + off[1])
            draw.polygon([a, b, (b[0], b[1] - wall_h), (a[0], a[1] - wall_h)],
                         fill=WALL_RGB + (205,), outline=WALL_EDGE + (150,))
            draw.line([(a[0], a[1] - wall_h), (b[0], b[1] - wall_h)],
                      fill=(250, 240, 255, 255), width=max(1, int(S * 10)))
        # Route, floating above the wall tops.
        for leg in legs:
            if leg["floor"] != f:
                continue
            poly = leg.get("polyline") or []
            if len(poly) < 2:
                continue
            flat = []
            up = []
            for p in poly:
                sp = to_small(f, p[0], p[1])
                q = _project(sp[0], sp[1], z, S)
                flat.append((q[0] + off[0] + 8, q[1] + off[1] + 10))
                q2 = _project(sp[0], sp[1], z + lift, S)
                up.append((q2[0] + off[0], q2[1] + off[1]))
            draw.line(flat, fill=(0, 0, 0, 90), width=max(2, int(S * 26)))
            draw.line(up, fill=(255, 255, 255, 255), width=max(3, int(S * 24)))
            draw.line(up, fill=ROUTE_RGB + (255,), width=max(2, int(S * 12)))
            if poly[0]:
                sp = to_small(f, poly[0][0], poly[0][1])
                q = _project(sp[0], sp[1], z + lift, S)
                draw.ellipse([q[0] + off[0] - 14, q[1] + off[1] - 14,
                              q[0] + off[0] + 14, q[1] + off[1] + 14],
                             fill=(34, 163, 85, 255), outline=(255, 255, 255, 255),
                             width=max(2, int(S * 5)))
            if poly[-1]:
                sp = to_small(f, poly[-1][0], poly[-1][1])
                q = _project(sp[0], sp[1], z + lift, S)
                draw.ellipse([q[0] + off[0] - 14, q[1] + off[1] - 14,
                              q[0] + off[0] + 14, q[1] + off[1] + 14],
                             fill=(221, 51, 51, 255), outline=(255, 255, 255, 255),
                             width=max(2, int(S * 5)))

    # Cross-floor connector between the two transit points.
    if len(legs) >= 2:
        t = (legs[0].get("transit") or legs[1].get("transit") or {})
        down = t.get("direction") == "down"

        def tp(leg, use_end):
            for s in (leg.get("steps") or []):
                if s.get("kind") == "transit" and s.get("point"):
                    return s["point"]
            p = leg.get("polyline") or []
            return p[-1] if use_end else (p[0] if p else None)

        pa = tp(legs[0], not down)
        pb = tp(legs[1], down)
        if pa and pb:
            fa, fb = legs[0]["floor"], legs[1]["floor"]
            za = z6 if fa == "6F" else 0
            zb = z6 if fb == "6F" else 0
            spa = to_small(fa, pa[0], pa[1])
            spb = to_small(fb, pb[0], pb[1])
            qa = _project(spa[0], spa[1], za, S)
            qb = _project(spb[0], spb[1], zb, S)
            qa = (qa[0] + off[0], qa[1] + off[1])
            qb = (qb[0] + off[0], qb[1] + off[1])
            draw.line([qa, qb], fill=(204, 0, 0, 255), width=max(3, int(S * 14)))
            for q in (qa, qb):
                draw.ellipse([q[0] - 12, q[1] - 12, q[0] + 12, q[1] + 12],
                             fill=(204, 0, 0, 255), outline=(255, 255, 255, 255),
                             width=max(2, int(S * 4)))

    # Floor name chips.
    font = _font(max(18, int(height * 0.035)))
    for f in floors_asc:
        z = z6 if f == "6F" else 0
        rects = outline(f)
        if not rects:
            continue
        r = rects[0]
        mid = _project((r[0] + r[2]) / 2, (r[1] + r[3]) / 2, z, S)
        cx = mid[0] + off[0]
        cy = mid[1] + off[1] - height * 0.07
        w = int(len(f) * font.size * 0.62) + 26
        h = int(font.size * 1.45)
        draw.rounded_rectangle([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
                               radius=8, fill=(17, 17, 17, 225))
        draw.text((cx, cy - h / 2 + 4), f, font=font, fill=(255, 255, 255, 255),
                  anchor="ma")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
