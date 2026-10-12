"""Compare corridors.json against the vector source (the .ai floor plans).

The raster audits can only say "the line is N px off the ink" and need a +-18
colour tolerance. The .ai carries the true geometry, so this tool pulls the wall
centre lines straight out of it and answers the same questions with exact
coordinates:

  * walls the drawing has but corridors.json does not  (the F1 blind spot)
  * walls corridors.json has but the drawing does not  (F5 phantoms)
  * the true displacement of each matched pair          (F5's 6-14 px offsets)
  * the drawing's door gaps, in pixel space             (F3 door verification)

It reuses the existing offline pipeline (scripts/p15_wall_draft_score.py reads
the .ai on stdlib alone; scripts/extract_walls.py turns it into wall centre
lines), so it adds no dependency.

Usage:
  PYTHONPATH=src python dev/vector_compare.py
  PYTHONPATH=src python dev/vector_compare.py --radius 40 --refine
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import library  # noqa: E402
from p15_wall_draft_score import (load_floor, GeoWalker, bright_bbox,  # noqa: E402
                                  make_transform)
from extract_walls import collect_items, merge_collinear  # noqa: E402

AI = {'5F': '5F Library Floor and Seats Map(SVG) copy.ai',
      '6F': '6F Library Floor and Seats Map(SVG) copy.ai'}


def dist_pt_seg(px, py, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def crosses(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def seg_seg_dist(a, b, c, d):
    if crosses(a, b, c, d):
        return 0.0
    return min(dist_pt_seg(*a, c, d), dist_pt_seg(*b, c, d),
               dist_pt_seg(*c, a, b), dist_pt_seg(*d, a, b))


def vector_walls(floor):
    """Wall centre lines of the .ai, in PDF space, plus its door gaps."""
    ai = os.path.join(ROOT, 'maps', AI[floor])
    fd = load_floor(ai)
    gw = GeoWalker(fd, ('on-d', 'on'))
    gw.run()
    fill, stroke, _ = collect_items(gw)
    merged, doors = merge_collinear(fill, stroke)
    all_gw = GeoWalker(fd, ('on-d', 'on', 'off'))
    all_gw.run()
    mb = fd['media_box']
    eps = 5.0
    inbox = [wp[1] for wp in all_gw.white_polys
             if wp[1][0] >= mb[0] - eps and wp[1][1] >= mb[1] - eps
             and wp[1][2] <= mb[2] + eps and wp[1][3] <= mb[3] + eps]
    white = max(inbox, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), default=tuple(mb))
    return merged, doors, white


def fit_axis(pairs, flip=False):
    """Least-squares y = c0 + c1 * x over (x, y) pairs."""
    n = len(pairs)
    if n < 2:
        return None
    sx = sum(p[0] for p in pairs)
    sy = sum(p[1] for p in pairs)
    sxx = sum(p[0] * p[0] for p in pairs)
    sxy = sum(p[0] * p[1] for p in pairs)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    c1 = (n * sxy - sx * sy) / den
    return (sy - c1 * sx) / n * 1.0, c1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--radius', type=float, default=40.0,
                    help='a wall counts as matched within this many px')
    ap.add_argument('--refine', action='store_true',
                    help='least-squares re-fit the transform before reporting')
    ap.add_argument('--top', type=int, default=10)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    for floor in ('5F', '6F'):
        cfg = library.CORRIDORS[floor]
        merged, doors, white = vector_walls(floor)
        base = library.map_path(cfg['map_file'])
        bb = bright_bbox(base)
        t, params = make_transform(white, bb[:4])
        vpx = [(t(a[0], a[1]), t(b[0], b[1])) for a, b in merged]
        data = [(tuple(p), tuple(q)) for p, q in cfg['walls']]
        print('\n=== %s: %d vector walls vs %d data walls ==='
              % (floor, len(vpx), len(data)))
        print('    coarse sheet-fit: sx=%.4f sy=%.4f px/unit' % (params[0], params[1]))

        rx0, ry0, rx1, ry1 = white
        ax0, bx0 = params[2] - rx0 * params[0], params[0]
        ay0, by0 = params[3] + ry1 * params[1], params[1]
        ax, bx, ay, by = ax0, bx0, ay0, by0

        def T(p):
            return (ax + bx * p[0], ay + by * (-p[1]))

        if args.refine:
            for it in range(4):
                vpx = [(T(a), T(b)) for a, b in merged]
                xpairs, ypairs = [], []
                for (ma, mb2), v in zip(merged, vpx):
                    best, bd = None, 1e9
                    for d in data:
                        dst = seg_seg_dist(v[0], v[1], d[0], d[1])
                        if dst < bd:
                            bd, best = dst, d
                    if bd > args.radius or best is None:
                        continue
                    if (abs(v[1][0] - v[0][0]) < abs(v[1][1] - v[0][1])) != \
                       (abs(best[1][0] - best[0][0]) < abs(best[1][1] - best[0][1])):
                        continue        # different orientation: not a pair
                    vmid = ((ma[0] + mb2[0]) / 2, (ma[1] + mb2[1]) / 2)
                    dmid = ((best[0][0] + best[1][0]) / 2,
                            (best[0][1] + best[1][1]) / 2)
                    if abs(v[1][0] - v[0][0]) < abs(v[1][1] - v[0][1]):
                        xpairs.append((vmid[0], dmid[0]))     # vertical -> x
                    else:
                        ypairs.append((-vmid[1], dmid[1]))    # horiz -> y (flip)
                fx, fy = fit_axis(xpairs), fit_axis(ypairs)
                if not fx or not fy:
                    print('    refine: only %d/%d pairs, keeping coarse transform'
                          % (len(xpairs), len(ypairs)))
                    break
                (ax, bx), (ay, by) = fx, fy
                print('    refine %d: X=%.1f%+.5f*x  Y=%.1f%+.5f*(-y)  '
                      '(%d x-pairs, %d y-pairs)' % (it + 1, ax, bx, ay, by,
                                                    len(xpairs), len(ypairs)))
            vpx = [(T(a), T(b)) for a, b in merged]
        else:
            vpx = [(T(a), T(b)) for a, b in merged]
        print('    coarse transform was X=%.1f%+.5f*x  Y=%.1f%+.5f*(-y)'
              % (ax0, bx0, ay0, by0))

        from PIL import Image
        W, H = Image.open(base).size

        def inplan(p):
            return 0 <= p[0] <= W and 0 <= p[1] <= H

        # --- the actionable output: how far is each DATA wall from the drawing?
        offs = []
        for d in data:
            dv = abs(d[1][0] - d[0][0]) < abs(d[1][1] - d[0][1])
            best, bd = None, 1e9
            for v in vpx:
                if (abs(v[1][0] - v[0][0]) < abs(v[1][1] - v[0][1])) != dv:
                    continue
                dst = seg_seg_dist(d[0], d[1], v[0], v[1])
                if dst < bd:
                    bd, best = dst, v
            if best is None or not inplan(d[0]):
                continue
            key = 0 if dv else 1          # vertical -> x offset, horizontal -> y
            bo = (((d[0][key] + d[1][key]) / 2)
                  - ((best[0][key] + best[1][key]) / 2))
            offs.append((abs(bo), bo, bd, d))
        if offs:
            mags = sorted(o[0] for o in offs)
            print('    data-wall displacement vs the drawing: median %.1f  p90 %.1f  '
                  'max %.1f px' % (mags[len(mags) // 2],
                                   mags[int(len(mags) * 0.9)], mags[-1]))
            print('    worst displaced data walls (apply these as shifts):')
            for ao, bo, bd, d in sorted(offs, reverse=True)[:args.top]:
                print('      off %+6.1f px  gap %5.1f px  len%6.0f  '
                      '(%6.0f,%6.0f)-(%6.0f,%6.0f)'
                      % (bo, bd, math.hypot(d[1][0] - d[0][0], d[1][1] - d[0][1]),
                         d[0][0], d[0][1], d[1][0], d[1][1]))

        # --- walls the drawing has but corridors.json does not (plan area only)
        missing, phantom = [], []
        for v in vpx:
            mid = ((v[0][0] + v[1][0]) / 2, (v[0][1] + v[1][1]) / 2)
            if not inplan(mid):
                continue
            bd = min(seg_seg_dist(v[0], v[1], d[0], d[1]) for d in data)
            if bd > args.radius:
                missing.append((bd, mid, v))
        for d in data:
            if not inplan(d[0]):
                continue
            bd = min(seg_seg_dist(d[0], d[1], v[0], v[1]) for v in vpx)
            if bd > args.radius:
                mid = ((d[0][0] + d[1][0]) / 2, (d[0][1] + d[1][1]) / 2)
                phantom.append((bd, mid, d))
        print('    drawing has, corridors.json does NOT, inside the sheet (%d):'
              % len(missing))
        for bd, mid, v in sorted(missing, reverse=True)[:args.top]:
            print('      %6.0f px away  len%6.0f  mid (%6.0f,%6.0f)'
                  % (bd, math.hypot(v[1][0] - v[0][0], v[1][1] - v[0][1]),
                     mid[0], mid[1]))
        print('    corridors.json has, drawing does NOT (%d):' % len(phantom))
        for bd, mid, d in sorted(phantom, reverse=True)[:args.top]:
            print('      %6.0f px away  len%6.0f  mid (%6.0f,%6.0f)'
                  % (bd, math.hypot(d[1][0] - d[0][0], d[1][1] - d[0][1]),
                     mid[0], mid[1]))
        print('    drawing door gaps (pixel space, half-width):')
        dd = []
        for d in doors:
            p = t(*d['position'])
            half = d['half_width'] * abs(params[0])
            dd.append((half, p))
        for half, p in sorted(dd, reverse=True)[:args.top]:
            print('      half %5.1f px  at (%6.0f,%6.0f)' % (half, p[0], p[1]))


if __name__ == '__main__':
    main()
