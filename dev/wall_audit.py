"""Offline wall / route audits (dev tool; stdlib + Pillow only).

Four cheap audits that each catch a class of data bug the route tests cannot:

  profile    per-wall 21-point perpendicular ink profile. Catches phantom walls
             (no ink under the line) and offset walls (ink exists but is N px
             off), and doubles as the F5 acceptance table.
  coverage   BIDIRECTIONAL wall coverage.
             (1) every data wall must have ink -> phantom detection.
             (2) every wall-coloured ink pixel must sit near some data wall ->
                 finds whole walls that were never drawn (the pre-F1 blind spot).
  ortho      spine edge orthogonality. Corridors are axis-aligned, so a
             non-axis-aligned edge is either a data error or must be listed in
             WHITELIST (this is exactly the F4 root cause: the wp1-wp2 diagonal).
  connect    grid free-space connectivity + per-point clearance. One pass checks
             every location and transit point can actually walk to the entrance,
             that doors are not welded shut (F3), that pockets are not sealed
             (F1), and that no point sits inside a wall (F2 / F2-audit).

Usage:
  PYTHONPATH=src python dev/wall_audit.py all
  PYTHONPATH=src python dev/wall_audit.py profile --floor 6F
"""
import argparse
import json
import math
import os
import sys
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

from PIL import Image, ImageChops, ImageDraw  # noqa: E402

import library  # noqa: E402

# Wall ink on both base maps is this colour family (sampled in the .ai-derived
# JPEGs); TOL is the per-channel tolerance, matching the value used since P1.
WALL_RGB = (114, 111, 154)
INK_TOL = 18
INK_DARK = 180          # a pixel counts as "line ink" below this luminance
PROFILE_RAD = 12        # perpendicular search radius for the profile pass
# The 5F sheet also carries real wall/partition bands drawn in LIGHT GREY
# (~luminance 198, e.g. the 15 px band under the hand-drawn wall at x~3097).
# At the 6F-tuned 180 cutoff those read as "no ink" and the wall looks like a
# phantom. 205 catches them; 6F is left untouched (verified 205 and 215 agree).
INK_DARK_BY_FLOOR = {'5F': 205, '6F': INK_DARK}

# Edges allowed to be non-axis-aligned. Purely visual spine shortcuts kept on
# purpose; anything else being diagonal means the spine grew a fabricated edge.
WHITELIST = {('6F', 'wp1', 'wp2')}


def load_corridors():
    return library.CORRIDORS


def floor_image(cfg, mode='RGB'):
    return Image.open(library.map_path(cfg['map_file'])).convert(mode)


def ink_mask(cfg):
    """mode '1': True where the pixel is wall-coloured ink."""
    base = floor_image(cfg, 'RGB')
    solid = Image.new('RGB', base.size, WALL_RGB)
    r, g, b = ImageChops.difference(base, solid).split()
    worst = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return worst.point(lambda v: 255 if v < INK_TOL else 0).convert('1')


def thick_ink_mask(cfg, kernel=5):
    """mode '1': ink that is at least `kernel` px across in BOTH axes.

    A door lintel / threshold is a 1-2 px line drawn across an opening (5F has
    one at y=754, 6F at y=764.6); eroding the ink with a kernel x kernel minimum
    filter deletes those while keeping real walls (8-11 px thick). Without this
    filter the coverage test flags every door as a wall the router cannot see.
    """
    from PIL import ImageFilter
    im = ink_mask(cfg).convert('L')
    return im.filter(ImageFilter.MinFilter(kernel)).point(
        lambda v: 255 if v else 0).convert('1')


def wall_mask(cfg, dilate):
    """mode '1': True within `dilate` px of any data wall segment."""
    im = Image.new('1', floor_image(cfg, 'L').size, 0)
    d = ImageDraw.Draw(im)
    for p, q in cfg['walls']:
        d.line([tuple(p), tuple(q)], fill=1, width=2 * dilate + 1)
    return im


def seg_point_dist(px, py, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_clearance(cfg, x, y):
    return min(seg_point_dist(x, y, tuple(p), tuple(q)) for p, q in cfg['walls'])


def seg_seg_dist(a, b, c, d):
    if library._segments_cross(a, b, c, d):
        return 0.0
    return min(seg_point_dist(*a, c, d), seg_point_dist(*b, c, d),
               seg_point_dist(*c, a, b), seg_point_dist(*d, a, b))


# ---------------------------------------------------------------- profile
def cmd_profile(args):
    floors = [args.floor] if args.floor else list(load_corridors())
    bad = 0
    for floor in floors:
        cfg = load_corridors()[floor]
        gray = floor_image(cfg, 'L').load()
        W, H = floor_image(cfg, 'L').size
        ink_dark = INK_DARK_BY_FLOOR.get(floor, INK_DARK)
        print('\n=== %s: %d walls, 21-point perpendicular ink profile ==='
              % (floor, len(cfg['walls'])))
        print('    ink%%  = share of samples with a line pixel within +-%d px of the '
              'line' % PROFILE_RAD)
        print('    off   = median signed distance to the nearest line pixel '
              '(0 = the line sits on ink; +N / -N = ink is N px off, so the data '
              'line is displaced by that much)')
        print('    depth = median contrast between the sample and the strongest '
              'neighbour (0 = flat ground)')
        rows = []
        for i, (a, b) in enumerate(cfg['walls']):
            n = 21
            dx, dy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(dx, dy)
            if ln < 1:
                continue
            ux, uy = -dy / ln, dx / ln
            hits, depths, offs, flags = 0, [], [], []
            for k in range(n):
                t = k / (n - 1)
                x, y = a[0] + t * dx, a[1] + t * dy
                c = gray[int(x), int(y)]
                best, first = 0, None
                for j in range(0, PROFILE_RAD + 1):
                    for jj in ((0,) if j == 0 else (j, -j)):
                        xx, yy = int(x + ux * jj), int(y + uy * jj)
                        if not (0 <= xx < W and 0 <= yy < H):
                            continue
                        v = gray[xx, yy]
                        best = max(best, v - c)
                        if first is None and v < ink_dark:
                            first = jj
                if first is not None:
                    hits += 1
                    offs.append(first)
                flags.append(first is not None)
                depths.append(best)
            depths.sort()
            hit = hits / n
            off = sorted(offs)[len(offs) // 2] if offs else 0
            # longest contiguous run of samples with no ink, in px. A doorway
            # shows up as a short gap; a wall that was never in the drawing (or
            # an overshoot stub) shows up as a long one.
            spacing = ln / (n - 1)
            run = best_run = 0
            for f in flags + [True]:
                if f:
                    best_run = max(best_run, run)
                    run = 0
                else:
                    run += 1
            rows.append((best_run * spacing, hit, depths[len(depths) // 2], off, i,
                         tuple(a), tuple(b), ln))
        rows.sort(key=lambda r: (-r[0], r[1]))
        records = []
        n_bare = 0
        for gap, hit, depth, off, i, a, b, ln in rows:
            tag = ''
            if hit < 0.8:
                tag = '  <<< mostly bare (phantom? or doorways)'
            elif abs(off) >= 4:
                tag = '  << DISPLACED'
            elif gap >= 120:
                tag = '  << long bare run (doorway / overshoot?)'
            if tag or args.verbose:
                print('   #%-2d ink%3.0f%% gap%5.0f off%+3d depth%4.0f len%6.0f '
                      '(%7.1f,%7.1f)-(%7.1f,%7.1f)%s'
                      % (i, hit * 100, gap, off, depth, ln,
                         a[0], a[1], b[0], b[1], tag))
            if hit < 0.8:
                n_bare += 1
            records.append({'index': i, 'a': [a[0], a[1]], 'b': [b[0], b[1]],
                            'length_px': round(ln, 1),
                            'ink_pct': round(hit * 100, 1),
                            'gap_px': round(gap, 1), 'off_px': off,
                            'depth': round(depth, 1), 'tag': tag.strip()})
        print('    -> %d/%d segments lack ink under the line (ink_dark=%d)'
              % (n_bare, len(rows), ink_dark))
        bad += n_bare
        if args.emit_json:
            out_dir = os.path.join(ROOT, 'extract_out', floor)
            os.makedirs(out_dir, exist_ok=True)
            payload = {'floor': floor, 'wall_count': len(cfg['walls']),
                       'generated_by': 'dev/wall_audit.py profile --emit-json',
                       'params': {'profile_rad': PROFILE_RAD, 'ink_dark': ink_dark,
                                  'ink_tol': INK_TOL, 'wall_rgb': list(WALL_RGB)},
                       'walls': records}
            path = os.path.join(out_dir, 'wall_profile.json')
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(payload, f, indent=1, ensure_ascii=False)
                f.write('\n')
            print('    wrote %s (%d walls)' % (path, len(records)))
    return 0


# ---------------------------------------------------------------- coverage
def _cluster(points, cell, min_cells):
    grid = {(x // cell, y // cell) for x, y in points}
    seen, out = set(), []
    for c in grid:
        if c in seen:
            continue
        q = deque([c])
        seen.add(c)
        comp = []
        while q:
            cx, cy = q.popleft()
            comp.append((cx, cy))
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    n2 = (cx + ddx, cy + ddy)
                    if n2 in grid and n2 not in seen:
                        seen.add(n2)
                        q.append(n2)
        if len(comp) >= min_cells:
            xs = [p[0] for p in comp]
            ys = [p[1] for p in comp]
            out.append((len(comp), (min(xs) * cell, min(ys) * cell,
                                    (max(xs) + 1) * cell, (max(ys) + 1) * cell)))
    out.sort(reverse=True)
    return out


def cmd_coverage(args):
    """(1) data-wall -> ink is `profile`'s job. (2) is what actually matters:

    the drawing is full of wall-coloured art that no route can ever hit (the
    building shell, rooms, shelves, the legend), so "every ink pixel needs a
    data wall" floods and is useless. The question a route cares about is
    narrower: **does a path or a corridor centreline run through ink that has no
    data wall behind it?** That is precisely how the pre-F1 data failed -- the
    route to the stairs walked straight through a real wall the data never had.
    """
    routes = []
    for start in (None, 'N607'):
        for r in library._load_rows():
            if r.get('type') == 'Transit':
                continue
            try:
                routes.append((start, library.build_route(r['name'], start=start)))
            except ValueError:
                pass
    masks = {}
    for floor, cfg in load_corridors().items():
        ink = thick_ink_mask(cfg, args.kernel)
        masks[floor] = (ink.load(), wall_mask(cfg, args.radius).load(), ink.size)
    for floor, cfg in load_corridors().items():
        if args.floor and floor != args.floor:
            continue
        ip, np_, (W, H) = masks[floor]
        print('\n=== %s: coverage of what paths actually touch ===' % floor)
        step = args.step

        def scan(p, q):
            ln = math.hypot(q[0] - p[0], q[1] - p[1])
            n = max(1, int(ln // step))
            hits = []
            for k in range(n + 1):
                t = k / n
                x, y = int(p[0] + t * (q[0] - p[0])), int(p[1] + t * (q[1] - p[1]))
                if 0 <= x < W and 0 <= y < H and ip[x, y] and not np_[x, y]:
                    hits.append((x, y))
            return hits

        wps = cfg['waypoints']
        spine_hits = 0
        for a_id, b_id in cfg['edges']:
            h = scan(tuple(wps[a_id]), tuple(wps[b_id]))
            if h:
                spine_hits += 1
                print('   spine edge %s-%s runs through %d unseen ink px, e.g. %s'
                      % (a_id, b_id, len(h), h[:3]))
        print('    spine edges crossing unseen ink: %d / %d'
              % (spine_hits, len(cfg['edges'])))

        bad, ok = [], 0
        for start, route in routes:
            worst = []
            for leg in route['legs']:
                if leg['floor'] != floor:
                    continue
                pts = [tuple(p) for p in leg['points']]
                for p, q in zip(pts, pts[1:]):
                    worst += scan(p, q)
            if worst:
                bad.append((len(worst), str(start), route['dest']['name'],
                            worst[:4]))
            else:
                ok += 1
        bad.sort(key=lambda z: -z[0])
        print('    routes on %s clean: %d; running through unseen ink: %d'
              % (floor, ok, len(bad)))
        for n, start, dest, egs in bad[:args.top]:
            print('      %4d px  start=%-6s %-28s e.g. %s'
                  % (n, start, dest[:28], egs))
    return 0


# ---------------------------------------------------------------- ortho
def cmd_ortho(args):
    """Flag edges whose ANGLE off the axes exceeds --tol-deg.

    Absolute skew is the wrong metric: a 1376 px corridor edge drawn with 88 px
    of jitter is still axis-aligned (3.7 deg). Angle separates that from a real
    diagonal like wp1-wp2 (36 deg)."""
    bad = 0
    for floor, cfg in load_corridors().items():
        if args.floor and floor != args.floor:
            continue
        wps = cfg['waypoints']
        print('\n=== %s: spine edge orthogonality (%d edges, flag > %g deg) ==='
              % (floor, len(cfg['edges']), args.tol_deg))
        angles = []
        flagged = []
        for a_id, b_id in cfg['edges']:
            a, b = tuple(wps[a_id]), tuple(wps[b_id])
            dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
            ln = math.hypot(dx, dy)
            if ln < 1:
                continue
            deg = math.degrees(math.atan2(min(dx, dy), max(dx, dy)))
            angles.append(deg)
            if deg <= args.tol_deg:
                continue
            if (floor, a_id, b_id) in WHITELIST or (floor, b_id, a_id) in WHITELIST:
                flagged.append((deg, a_id, b_id, ln, a, b, True))
            else:
                bad += 1
                flagged.append((deg, a_id, b_id, ln, a, b, False))
        angles.sort()
        for deg, a_id, b_id, ln, a, b, white in sorted(flagged, reverse=True):
            tag = '[WHITELISTED diagonal]' if white else '<<< NOT WHITELISTED'
            print('   %-6s-%-6s len%6.0f  %5.1f deg  %-24s (%d,%d)-(%d,%d)'
                  % (a_id, b_id, ln, deg, tag, a[0], a[1], b[0], b[1]))
        print('    angles: median %.1f deg, p90 %.1f deg, max %.1f deg'
              % (angles[len(angles) // 2], angles[int(len(angles) * 0.9)], angles[-1]))
        print('    -> %d non-whitelisted edge(s) over %g deg'
              % (bad, args.tol_deg))
    return 0


# ---------------------------------------------------------------- connect
def cmd_connect(args):
    rows = [r for r in library._load_rows()]
    for floor, cfg in load_corridors().items():
        if args.floor and floor != args.floor:
            continue
        step = args.step
        blocked = wall_mask(cfg, args.clearance)
        bp = blocked.load()
        W, H = blocked.size
        gw, gh = W // step + 1, H // step + 1

        def cell(x, y):
            return (int(round(x)) // step, int(round(y)) // step)

        def free(cx, cy):
            if not (0 <= cx < gw and 0 <= cy < gh):
                return False
            return not bp[min(cx * step, W - 1), min(cy * step, H - 1)]

        def snap(x, y, rad):
            """nearest free cell to (x, y) within `rad` cells, or None."""
            cx, cy = cell(x, y)
            if free(cx, cy):
                return (cx, cy)
            for r in range(1, rad + 1):
                for ddx in range(-r, r + 1):
                    for ddy in range(-r, r + 1):
                        if max(abs(ddx), abs(ddy)) != r:
                            continue
                        if free(cx + ddx, cy + ddy):
                            return (cx + ddx, cy + ddy)
            return None

        # anchors: 5F walks in from the entrance; 6F is only reachable via transit
        anchors = []
        if floor == '5F':
            for r in rows:
                if r.get('type') != 'Transit' and 'Entrance' in (r.get('name') or ''):
                    anchors.append((r['name'], float(r['x']), float(r['y'])))
        for r in rows:
            if r.get('type') == 'Transit' and r.get('floor') == floor:
                anchors.append((r['name'] + ' (transit)', float(r['x']), float(r['y'])))
        if not anchors:
            print('\n[warn] %s: no anchor point' % floor)
            continue
        seen = set()
        roots = []
        for name, x, y in anchors:
            c = snap(x, y, args.snap_rad)
            if c is None:
                print('   anchor %-24s has NO free cell within %d cells '
                      '(inside a wall / no clearance)' % (name, args.snap_rad))
                continue
            if c in seen:
                continue
            q = deque([c])
            seen.add(c)
            while q:
                cx, cy = q.popleft()
                for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    n2 = (cx + ddx, cy + ddy)
                    if n2 not in seen and free(*n2):
                        seen.add(n2)
                        q.append(n2)
            roots.append((name, c))

        print('\n=== %s: free-space connectivity (step %dpx, clearance %dpx) ==='
              % (floor, step, args.clearance))
        print('    anchors: %s' % ', '.join('%s@%s' % (n, c) for n, c in roots))
        allfree = [(cx, cy) for cx in range(gw) for cy in range(gh) if free(cx, cy)]
        print('    reachable cells from anchors: %d of %d free' % (len(seen), len(allfree)))
        # sealed pockets: free space the walker can never enter
        pset = {c for c in allfree if c not in seen}
        comps, seenp = [], set()
        for c in pset:
            if c in seenp:
                continue
            q = deque([c])
            seenp.add(c)
            comp = []
            while q:
                cx, cy = q.popleft()
                comp.append((cx, cy))
                for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    n2 = (cx + ddx, cy + ddy)
                    if n2 in pset and n2 not in seenp:
                        seenp.add(n2)
                        q.append(n2)
            comps.append(comp)
        comps.sort(key=len, reverse=True)
        print('    SEALED POCKETS (free space unreachable from any anchor): %d, '
              'largest %d cells' % (len(comps), len(comps[0]) if comps else 0))
        for comp in comps[:6]:
            xs = [p2[0] * step for p2 in comp]
            ys = [p2[1] * step for p2 in comp]
            if len(comp) < 4:
                continue
            print('      %6d cells  bbox (%5d,%5d)-(%5d,%5d)  centre (%5.0f,%5.0f)'
                  % (len(comp), min(xs), min(ys), max(xs), max(ys),
                     sum(xs) / len(xs), sum(ys) / len(ys)))
        unreachable, no_room, tight = [], [], []
        for r in rows:
            if r.get('floor') != floor:
                continue
            x, y = float(r['x']), float(r['y'])
            clr = point_clearance(cfg, x, y)
            c = snap(x, y, args.snap_rad)
            label = '%-28s %-4s (%6.0f,%6.0f)' % (r['name'][:28], r.get('type'),
                                                  x, y)
            if c is None:
                no_room.append((clr, label))
            elif c not in seen:
                unreachable.append((clr, label, c))
            if clr < args.clearance:
                tight.append((clr, label))
        print('    points with NO free cell (inside wall / no clearance): %d'
              % len(no_room))
        for clr, label in sorted(no_room)[:args.top]:
            print('      clearance %5.1f px  %s' % (clr, label))
        print('    points NOT connected to an anchor: %d' % len(unreachable))
        for clr, label, c in sorted(unreachable)[:args.top]:
            print('      clearance %5.1f px  cell %s  %s' % (clr, c, label))
        print('    points closer than %d px to a wall: %d' % (args.clearance, len(tight)))
        for clr, label in sorted(tight)[:args.top]:
            print('      clearance %5.1f px  %s' % (clr, label))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('profile', 'coverage', 'ortho', 'connect', 'all'))
    ap.add_argument('--floor', default=None, help='limit to one floor (5F/6F)')
    ap.add_argument('--radius', type=int, default=25,
                    help='coverage: ink must be within this many px of a wall')
    ap.add_argument('--step', type=int, default=None,
                    help='sampling step (coverage default 6, connect default 10)')
    ap.add_argument('--min-cells', type=int, default=25,
                    help='coverage: report clusters with at least this many cells')
    ap.add_argument('--top', type=int, default=12, help='rows per report')
    ap.add_argument('--tol-deg', type=float, default=10.0,
                    help='ortho: flag edges this many degrees off the axes')
    ap.add_argument('--kernel', type=int, default=5,
                    help='coverage: min ink thickness px (kills door lintels)')
    ap.add_argument('--corridor', type=int, default=80,
                    help='coverage: walking band half-width around the spine px')
    ap.add_argument('--clearance', type=int, default=6,
                    help='connect: required free clearance px')
    ap.add_argument('--snap-rad', type=int, default=4,
                    help='connect: search radius (cells) for a free cell')
    ap.add_argument('--emit-json', action='store_true',
                    help='profile: also write extract_out/<floor>/wall_profile.json')
    ap.add_argument('--verbose', action='store_true', help='profile: print every row')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if args.step is None:
        args.step = 6 if args.cmd == 'coverage' else 10

    if args.cmd == 'all':
        for fn in (cmd_ortho, cmd_profile, cmd_coverage, cmd_connect):
            fn(args)
    else:
        {'profile': cmd_profile, 'coverage': cmd_coverage,
         'ortho': cmd_ortho, 'connect': cmd_connect}[args.cmd](args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
