"""Trace the building outline (floor-plan area) of each base map.

The base maps carry a legend / title / compass band below the actual plan.
On 6F that band is ~55% of the image; including it in the 3D slab blows the
slab up and pushes the route off-centre. The 5F plan is L-shaped, so a plain
rectangular crop is wrong there.

Method: wall-coloured ink marks the structural lines of the plan. For each
scan row we take the span of ink that belongs to the *building* (a long
contiguous run), which excludes isolated legend glyphs. The outline is then
the union of those spans, simplified into a rectangle list.

Prints a Python literal for src/view/plan_outline.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import library  # noqa: E402
from PIL import Image  # noqa: E402

WALL_RGB = (114, 111, 154)
TOL = 25
# A run of wall ink at least this long (in sampled steps) is structure, not
# a legend glyph. Sampled every STEP px, so 40 samples == 240 full-res px.
MIN_RUN = 40


def main():
    for floor in ('5F', '6F'):
        cfg = library.CORRIDORS[floor]
        im = Image.open(library.map_path(cfg['map_file'])).convert('RGB')
        W, H = im.size
        px = im.load()

        def isw(x, y):
            if not (0 <= x < W and 0 <= y < H):
                return False
            c = px[x, y]
            return (abs(c[0] - WALL_RGB[0]) < TOL
                    and abs(c[1] - WALL_RGB[1]) < TOL
                    and abs(c[2] - WALL_RGB[2]) < TOL)

        STEP = 6
        print('=== %s  %dx%d ===' % (floor, W, H))
        spans = []
        for y in range(0, H, 12):
            runs = []
            start = None
            for x in range(0, W, STEP):
                if isw(x, y):
                    if start is None:
                        start = x
                else:
                    if start is not None:
                        if (x - start) // STEP >= MIN_RUN:
                            runs.append((start, x - STEP))
                        start = None
            if start is not None and (W - start) // STEP >= MIN_RUN:
                runs.append((start, W - STEP))
            if runs:
                spans.append((y, runs))
        # Merge adjacent rows that share a similar span into bands.
        bands = []
        for y, runs in spans:
            for b in bands:
                if abs(b['y1'] - y) <= 12:
                    b['y1'] = y
                    b['runs'].append(runs)
                    break
            else:
                bands.append({'y0': y, 'y1': y, 'runs': [runs]})
        for b in bands:
            lo = min(r[0] for rs in b['runs'] for r in rs)
            hi = max(r[1] for rs in b['runs'] for r in rs)
            print('   band y[%4d..%4d]  x[%4d..%4d]' % (b['y0'], b['y1'], lo, hi))
        print()


if __name__ == '__main__':
    main()
