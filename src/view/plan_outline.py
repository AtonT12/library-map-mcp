"""Building outline of each floor base map, in full-res pixel coords.

The base maps carry a legend / title / compass band below the actual floor
plan (on 6F that band is ~55% of the image height). The 3D slab is drawn from
this outline so the legend does not stretch the model and push the route
off-centre.

Each floor is a list of axis-aligned rectangles that together cover the plan
area. The 5F plan is L-shaped (a full-width north wing plus a narrow west
wing), the 6F plan is a single rectangle.

Measured from the wall-coloured structural ink of the base maps: per scan
row, the long contiguous ink run that belongs to the building (isolated
legend glyphs are excluded), unioned into the rectangles below.
"""

# floor -> list of (x0, y0, x1, y1) in full-res pixel coords
#
# The 5F plan is L-shaped, so it needs two rectangles. They must share the
# seam exactly: the north wing ends at y=1900 and the west wing starts at
# y=1899, so the projected edges meet without a gap (a one-pixel overlap
# closes the seam; the same y on both sides would leave a visible crack
# once the oblique projection shears them apart).
PLAN_OUTLINE = {
    "5F": [
        (0, 0, 6062, 1900),        # north wing: full width
        (0, 1899, 1400, 4205),     # west wing: narrow column to the bottom
    ],
    "6F": [
        (0, 0, 7534, 1760),        # single rectangle; legend starts below
    ],
}


def outline_bbox(floor):
    """Union bbox of the outline rectangles for a floor."""
    rects = PLAN_OUTLINE[floor]
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def covers(floor, x, y):
    """True when a full-res point falls inside the plan outline."""
    return any(r[0] <= x <= r[2] and r[1] <= y <= r[3]
               for r in PLAN_OUTLINE[floor])
