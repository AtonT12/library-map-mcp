r"""P2 wall extractor (offline dev tool, stdlib + Pillow only).

Reads a .ai floor plan, pulls wall-colored geometry from the P1.5-frozen ON
layer set, converts thick fill rects to centerlines, de-duplicates stroke
centerlines against fill centerlines, merges collinear runs, detects door
gaps, maps everything through the P2.0 coarse transform, and emits:

  walls_pdf.json                 - PDF-space segments, schema == corridors.json walls
  walls_px_draft.json            - same, mapped to base-image pixel space (coarse)
  report.json                    - stats, doors, thickness, transform, margins
  route_override_candidates.json - proposed moves for Shelf points sitting on lines
  debug.png                      - base image + extracted walls + doors + hand baseline

Usage:
  python extract_walls.py --floor 5F \
      --ai "..\maps\5F Library Floor and Seats Map(SVG) copy.ai" \
      --base ..\maps\5f_base.jpg --corridors ..\corridors.json --csv ..\locations.csv \
      --out-dir ..\extract_out\5F

Re-run after P3 with --transform <json {sx,sy,px0,py0,pdf_rect}> to refresh
pixel artifacts without re-deriving geometry.
"""
import argparse
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p15_wall_draft_score import load_floor, GeoWalker, bright_bbox, make_transform

# Plan constants (kept in sync with src/test.py L179-180)
WALL_CLEARANCE_PX = 6
SPINE_MIN_MARGIN_PX = 20
REPOINT_MIN_DIST = WALL_CLEARANCE_PX + SPINE_MIN_MARGIN_PX + 2

MERGE_SEAM_TOL = 6.0    # pdf units: gap absorbed when joining collinear runs
DOOR_GAP_MIN = 8.0      # pdf units: smaller gaps are seams, not doors
DOOR_GAP_MAX = 160.0    # pdf units: bigger gaps = separate wall pieces
C_TOL = 6.0             # pdf units: intercept tolerance for line clustering
MIN_SEG_LEN = 4.0


def dist(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)


def dist_pt_seg(px, py, a, b):
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    if l2 == 0:
        return dist(px, py, ax, ay)
    u = ((px - ax) * vx + (py - ay) * vy) / l2
    u = max(0.0, min(1.0, u))
    return math.hypot(px - (ax + u * vx), py - (ay + u * vy))


def quad_midline(pts):
    """Centerline of a 4-corner poly (post-CTM) + its short-side thickness."""
    if len(pts) != 4:
        return None
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    ordered = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    q = [tuple(p) for p in ordered]
    lens = [dist(q[i][0], q[i][1], q[(i + 1) % 4][0], q[(i + 1) % 4][1])
            for i in range(4)]
    if lens[0] + lens[2] >= lens[1] + lens[3]:
        # long edges e0/q0-q1 and e2/q2-q3: centerline runs between the
        # midpoints of the two short edges, thickness = mean short-edge length
        ma = ((q[1][0] + q[2][0]) / 2.0, (q[1][1] + q[2][1]) / 2.0)
        mb = ((q[3][0] + q[0][0]) / 2.0, (q[3][1] + q[0][1]) / 2.0)
        thick = (lens[1] + lens[3]) / 2.0
    else:
        ma = ((q[0][0] + q[1][0]) / 2.0, (q[0][1] + q[1][1]) / 2.0)
        mb = ((q[2][0] + q[3][0]) / 2.0, (q[2][1] + q[3][1]) / 2.0)
        thick = (lens[0] + lens[2]) / 2.0
    if thick <= 0 or dist(*ma, *mb) < MIN_SEG_LEN:
        return None
    return ((round(ma[0], 2), round(ma[1], 2)),
            (round(mb[0], 2), round(mb[1], 2)), thick)


def collect_items(gw):
    """Wall-color fills -> quad centerlines; strokes -> raw sub-segments."""
    fill_segs = []
    stroke_segs = []
    skipped_fill = 0
    for rec in gw.poly_fills:
        res = quad_midline(rec['pts'])
        if res is None:
            skipped_fill += 1
            continue
        a, b, thick = res
        fill_segs.append({'a': a, 'b': b, 'thick': thick, 'layer': rec['layer']})
    for rec in gw.line_strokes:
        pts = rec['pts']
        for p, q in zip(pts[:-1], pts[1:]):
            if dist(*p, *q) > MIN_SEG_LEN:
                stroke_segs.append({'a': p, 'b': q, 'layer': rec['layer']})
    return fill_segs, stroke_segs, skipped_fill


def stroke_covered(s, fill_segs, tol):
    ax, ay = s['a']
    bx, by = s['b']
    slen = dist(ax, ay, bx, by)
    if slen <= 0:
        return True
    ux, uy = (bx - ax) / slen, (by - ay) / slen
    for f in fill_segs:
        fx, fy = f['a']
        gx, gy = f['b']
        fslen = dist(fx, fy, gx, gy)
        if fslen <= 0:
            continue
        dot = ((bx - ax) * (gx - fx) + (by - ay) * (gy - fy)) / (slen * fslen)
        if abs(dot) < 0.85:
            continue
        d = min(dist_pt_seg(ax, ay, f['a'], f['b']),
                dist_pt_seg(bx, by, f['a'], f['b']),
                dist_pt_seg(fx, fy, (ax, ay), (bx, by)),
                dist_pt_seg(gx, gy, (ax, ay), (bx, by)))
        if d > tol:
            continue
        ov = max((fx - ax) * ux + (fy - ay) * uy,
                 (gx - ax) * ux + (gy - ay) * uy)
        if ov >= 0.5 * slen:
            return True
    return False


def merge_collinear(fill_segs, stroke_segs):
    """Group near-collinear segments, merge into maximal runs, flag gaps as doors.

    Returns (merged_segments [(a,b),(a,b)...], doors [{...}...]).
    """
    buckets = {}
    for f in fill_segs + [{'a': s['a'], 'b': s['b']} for s in stroke_segs]:
        ax, ay = f['a']
        bx, by = f['b']
        L = dist(ax, ay, bx, by)
        if L < MIN_SEG_LEN:
            continue
        th = math.atan2(by - ay, bx - ax) % math.pi
        tq = int(round(math.degrees(th)))
        ux, uy = math.cos(th), math.sin(th)
        ta = ax * ux + ay * uy
        tb = bx * ux + by * uy
        if ta > tb:
            ta, tb = tb, ta
        c = ax * uy - ay * ux
        buckets.setdefault((tq, int(round(c / C_TOL))), []).append(
            (ta, tb, c, (ax, ay)))

    merged = []
    doors = []
    for (tq, _ck), lst in buckets.items():
        th = tq * math.pi / 180.0
        ux, uy = math.cos(th), math.sin(th)
        clusters = []
        for item in sorted(lst, key=lambda v: v[2]):
            if clusters and item[2] - clusters[-1][-1][2] <= C_TOL:
                clusters[-1].append(item)
            else:
                clusters.append([item])
        for cl in clusters:
            cs = sum(v[2] for v in cl) / len(cl)
            intervals = sorted((v[0], v[1]) for v in cl)
            cur = list(intervals[0])
            doors_here = []
            runs = []
            for ta, tb in intervals[1:]:
                gap = ta - cur[1]
                if gap <= MERGE_SEAM_TOL:
                    cur[1] = max(cur[1], tb)
                else:
                    runs.append((cur[0], cur[1]))
                    if DOOR_GAP_MIN <= gap <= DOOR_GAP_MAX:
                        doors_here.append((cur[1], ta, cur[0]))
                    cur = [ta, tb]
            runs.append((cur[0], cur[1]))
            for t0, t1 in runs:
                if t1 - t0 < MIN_SEG_LEN:
                    continue
                # point on line = t*u - c*n with n = (-uy, ux) since c = p·(-u rotated)
                a = (t0 * ux + cs * uy, t0 * uy - cs * ux)
                b = (t1 * ux + cs * uy, t1 * uy - cs * ux)
                merged.append(((round(a[0], 1), round(a[1], 1)),
                               (round(b[0], 1), round(b[1], 1))))
            for cur_end, nxt_start, run_start in doors_here:
                gc = (cur_end + nxt_start) / 2.0
                pos = (gc * ux + cs * uy, gc * uy - cs * ux)
                doors.append({
                    'position': [round(pos[0], 1), round(pos[1], 1)],
                    'half_width': round((nxt_start - cur_end) / 2.0, 2),
                    'theta_deg': round(math.degrees(th), 1),
                    'left_run_len': round(cur_end - run_start, 1),
                })
    return merged, doors


def map_segment(a, b, tfunc):
    pa = tfunc(*a)
    pb = tfunc(*b)
    return ((round(pa[0], 1), round(pa[1], 1)), (round(pb[0], 1), round(pb[1], 1)))


def segs_to_pairs(segs):
    return [[[a[0], a[1]], [b[0], b[1]]] for a, b in segs]


def _place_ids(rows):
    """Place ids via library._assign_place_ids when importable (same scope rule)."""
    src_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'src')
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        import library
        return list(zip(library._assign_place_ids(rows), rows))
    except Exception:
        import re as _re
        counts = {}
        out = []
        for r in rows:
            key = (r.get('call_start') or '').strip()
            floor = (r.get('floor') or '').strip().lower()
            if key and key.upper() != 'NULL':
                base = key.lower()
            else:
                base = _re.sub(r'[^a-z0-9]+', '-', (r.get('name') or '').lower()) \
                    .strip('-')
            base = f'{base}-{floor}' if floor else base
            n = counts.get(base, 0) + 1
            counts[base] = n
            out.append((base if n == 1 else f'{base}-{n}', r))
        return out


def shelf_candidates(csv_path, floor, walls_px, gray_im, im_size, base_name):
    """Shelf points sitting closer than REPOINT_MIN_DIST to any wall line get a
    proposed move (open side chosen by local dark-pixel density). Returns
    (candidates, flagged). Coarse-transform based: refresh after P3."""
    import csv
    from PIL import ImageFilter
    with open(csv_path, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    paired = _place_ids(rows)
    W, H = im_size
    dpx = gray_im.filter(ImageFilter.MinFilter(7)).load()
    cands = []
    flagged = []
    for pid, row in paired:
        if row.get('type') != 'Shelf' or row.get('floor') != floor:
            continue
        if row.get('map_file', '').lower() != base_name.lower():
            continue
        try:
            px, py = int(row['x']), int(row['y'])
        except ValueError:
            continue
        best_d, best_q, best_i = None, None, None
        for i, (a, b) in enumerate(walls_px):
            ax, ay = a
            bx, by = b
            vx, vy = bx - ax, by - ay
            l2 = vx * vx + vy * vy
            if l2 == 0:
                continue
            u = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
            qx, qy = ax + u * vx, ay + u * vy
            d = math.hypot(px - qx, py - qy)
            if best_d is None or d < best_d:
                best_d, best_q, best_i = d, (qx, qy), i
        if best_d is None or best_d >= REPOINT_MIN_DIST:
            continue
        L = dist(px, py, *best_q) or 1.0
        nx, ny = (px - best_q[0]) / L, (py - best_q[1]) / L
        delta = REPOINT_MIN_DIST - best_d + 2.0
        picks = []
        for sgn in (1.0, -1.0):
            cx_ = px + nx * sgn * delta
            cy_ = py + ny * sgn * delta
            if not (2 <= cx_ <= W - 3 and 2 <= cy_ <= H - 3):
                continue
            s = c = 0
            for yy in range(int(cy_) - 18, int(cy_) + 19, 4):
                for xx in range(int(cx_) - 18, int(cx_) + 19, 4):
                    if 0 <= xx < W and 0 <= yy < H:
                        s += dpx[xx, yy]
                        c += 1
            clear = all(dist_pt_seg(cx_, cy_, a, b) >= REPOINT_MIN_DIST
                         for a, b in walls_px)
            picks.append((s / max(1, c), not clear, cx_, cy_))
        entry = {'place_id': pid, 'name': row.get('name'), 'old': [px, py],
                 'wall_index': best_i, 'dist_before_px': round(best_d, 1)}
        if not picks:
            entry['flag'] = 'no in-bounds side'
            flagged.append(entry)
            continue
        _, bad_clear, cx_, cy_ = sorted(picks)[0]
        entry['new'] = [int(round(cx_)), int(round(cy_))]
        if bad_clear:
            entry['needs_review'] = 'chosen side still violates clearance'
            flagged.append(entry)
        cands.append(entry)
    return cands, flagged


def regression_vs_hand(corridor, floor, walls_px):
    """P3 free-verification #1 (coarse version): distance of each hand-drawn wall
    to the nearest extracted segment."""
    if not corridor:
        return None
    hands = (corridor.get(floor) or {}).get('walls') or []
    if not hands or not walls_px:
        return None
    per = []
    for hi, w in enumerate(hands):
        ax, ay = [float(v) for v in w[0]]
        bx, by = [float(v) for v in w[1]]
        L = dist(ax, ay, bx, by)
        n = int(min(24, max(4, L / 60)))
        ds = []
        for k in range(n + 1):
            f = k / n
            X = ax + (bx - ax) * f
            Y = ay + (by - ay) * f
            ds.append(min((dist_pt_seg(X, Y, a, b) for a, b in walls_px),
                          default=float('nan')))
        per.append({'hand_wall': hi, 'median_px': round(statistics.median(ds), 1),
                    'max_px': round(max(ds), 1)})
    meds = [o['median_px'] for o in per]
    return {'per_wall': per, 'median_of_medians_px': round(statistics.median(meds), 1)}


def min_spine_margin(corridor, floor, walls_px):
    """Provisional post-extraction minimum spine-to-wall distance (§2.3 input;
    P3 recomputes with the calibrated transform)."""
    fdata = corridor.get(floor) or {} if corridor else {}
    wp = fdata.get('waypoints') or {}
    edges = fdata.get('edges') or []
    best = None
    for e in edges:
        a, b = wp.get(e[0]), wp.get(e[1])
        if not a or not b:
            continue
        L = dist(a[0], a[1], b[0], b[1])
        n = max(2, int(L / 40))
        for k in range(n + 1):
            f = k / n
            X = a[0] + (b[0] - a[0]) * f
            Y = a[1] + (b[1] - a[1]) * f
            dmin = min((dist_pt_seg(X, Y, p, q) for p, q in walls_px),
                       default=float('inf'))
            if best is None or dmin < best:
                best = dmin
    return round(best, 1) if best is not None else None


def draw_debug(base_path, walls_px, doors, tfunc, hand_walls, cands, out_png,
               scale=0.22):
    from PIL import Image, ImageDraw
    im = Image.open(base_path).convert('RGB')
    W, H = im.size
    im2 = im.resize((max(1, int(W * scale)), max(1, int(H * scale))))
    d = ImageDraw.Draw(im2)
    for w in (hand_walls or []):
        p = ([float(v) for v in w[0]][0] * scale, [float(v) for v in w[0]][1] * scale)
        q = ([float(v) for v in w[1]][0] * scale, [float(v) for v in w[1]][1] * scale)
        d.line([p, q], fill='green', width=max(1, int(2 * scale)))
    for a, b in walls_px:
        d.line([(a[0] * scale, a[1] * scale), (b[0] * scale, b[1] * scale)],
               fill='red', width=max(1, int(2.4 * scale)))
    for door in doors:
        cx, cy = tfunc(*door['position'])
        d.ellipse([cx * scale - 5, cy * scale - 5, cx * scale + 5, cy * scale + 5],
                  outline='blue', width=2)
    for c in cands:
        if 'new' not in c:
            continue
        o = (c['old'][0] * scale, c['old'][1] * scale)
        nw = (c['new'][0] * scale, c['new'][1] * scale)
        d.line([o, nw], fill='magenta', width=2)
        d.ellipse([nw[0] - 4, nw[1] - 4, nw[0] + 4, nw[1] + 4], fill='magenta')
    im2.save(out_png)
    return out_png


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--floor', required=True)
    ap.add_argument('--ai', required=True)
    ap.add_argument('--base', required=True)
    ap.add_argument('--corridors', default=None)
    ap.add_argument('--csv', default=None)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--transform', default=None,
                    help='JSON {sx,sy,px0,py0,pdf_rect:[rx0,ry0,...]} from P3')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    os.makedirs(args.out_dir, exist_ok=True)
    corridor = json.load(open(args.corridors, encoding='utf-8')) \
        if args.corridors else None

    floordata = load_floor(args.ai)
    gw = GeoWalker(floordata, ('on-d', 'on'))
    gw.run()
    state_counts = {s: sum(1 for L in floordata['layers'] if L['state'] == s)
                    for s in ('on-d', 'on', 'off')}
    print('layer states:', state_counts)
    fill_segs, stroke_segs, skipped_fill = collect_items(gw)
    thick_samples = [f['thick'] for f in fill_segs]
    med_thick = statistics.median(thick_samples) if thick_samples else 0.0
    dup_tol = max(6.0, min(20.0, med_thick * 2.0))
    kept_strokes = [s for s in stroke_segs
                    if not stroke_covered(s, fill_segs, dup_tol)]
    n_dropped = len(stroke_segs) - len(kept_strokes)
    print('fills=%d (nonquad skipped %d) | strokes=%d (dup dropped %d) | '
          'wall thickness median=%.1f pdf units (dup tol %.1f)' %
          (len(fill_segs), skipped_fill, len(stroke_segs), n_dropped,
           med_thick, dup_tol))

    merged, doors = merge_collinear(fill_segs, kept_strokes)

    all_gw = GeoWalker(floordata, ('on-d', 'on', 'off'))
    all_gw.run()
    mb = floordata['media_box']
    eps = 5.0
    in_box = [wp[1] for wp in all_gw.white_polys
              if wp[1][0] >= mb[0] - eps and wp[1][1] >= mb[1] - eps
              and wp[1][2] <= mb[2] + eps and wp[1][3] <= mb[3] + eps]
    white_rect = max(in_box,
                     key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), default=None)
    if white_rect is None:
        white_rect = tuple(mb)
    pad = 40.0
    wx0, wy0, wx1, wy1 = white_rect
    def inside(p):
        return (wx0 - pad <= p[0] <= wx1 + pad
                and wy0 - pad <= p[1] <= wy1 + pad)

    in_sheet = [(a, b) for a, b in merged if inside(a) and inside(b)]
    n_outsheet = len(merged) - len(in_sheet)
    doors = [d for d in doors
             if wx0 - pad <= d['position'][0] <= wx1 + pad
             and wy0 - pad <= d['position'][1] <= wy1 + pad]
    doors.sort(key=lambda d: d['half_width'])
    min_door_half = min((d['half_width'] for d in doors), default=None)
    print('merged runs=%d (out-of-sheet dropped %d) | door gaps=%d | '
          'min half-width=%s pdf units' %
          (len(merged), n_outsheet, len(doors),
           ('%.1f' % min_door_half) if min_door_half is not None else 'n/a'))
    if args.transform:
        tr = json.load(open(args.transform, encoding='utf-8'))
        sx, sy, px0, py0 = tr['sx'], tr['sy'], tr['px0'], tr['py0']
        rx0, ry0 = tr['pdf_rect'][0], tr['pdf_rect'][1]

        def t(x, y):
            return (px0 + (x - rx0) * sx, py0 + (ry0 - y) * sy)

        params = (sx, sy, px0, py0, rx0, ry0)
        print('transform: loaded from %s' % args.transform)
    else:
        bb = bright_bbox(args.base)
        pix_bbox, _im = bb[:4], bb[4]
        t, params = make_transform(white_rect, pix_bbox)
        print('coarse transform: sx=%.3f sy=%.3f px/unit (unif residual %.2f%%)' %
              (params[0], params[1],
               abs(params[0] - params[1]) / max(params[0], params[1]) * 100))

    walls_pdf = in_sheet
    walls_px = [map_segment(a, b, t) for a, b in walls_pdf]
    base_name = os.path.basename(args.base)

    reg = regression_vs_hand(corridor, args.floor, walls_px)
    margin = min_spine_margin(corridor, args.floor, walls_px)
    if reg:
        print('regression vs hand walls: median-of-medians %.1f px' %
              reg['median_of_medians_px'])
        for o in reg['per_wall']:
            print('  hand wall %d: median %.1f px  max %.1f px' %
                  (o['hand_wall'], o['median_px'], o['max_px']))
    print('provisional min spine margin: %s px' % margin)

    cands, flagged = [], []
    gray_im = None
    if args.csv:
        from PIL import Image
        gray_im = Image.open(args.base).convert('L')
        cands, flagged = shelf_candidates(args.csv, args.floor, walls_px,
                                          gray_im, gray_im.size, base_name)
        print('shelf override candidates: %d (flagged %d)' %
              (len(cands), len(flagged)))
        for c in cands:
            note = '  [%s]' % c.get('needs_review', '') if c.get('needs_review') else ''
            print('  %s %-28s %s -> %s%s' %
                  (c['place_id'], (c['name'] or '')[:28], c['old'], c['new'], note))
        for fl in flagged:
            if 'new' not in fl:
                print('  FLAGGED %s %s @%s (%s)' %
                      (fl['place_id'], fl.get('name'), fl['old'], fl.get('flag')))

    hand_walls = (corridor or {}).get(args.floor, {}).get('walls') or []
    dbg = os.path.join(args.out_dir, 'debug.png')
    draw_debug(args.base, walls_px, doors, t, hand_walls, cands, dbg)

    report = {
        'floor': args.floor,
        'source_ai': os.path.basename(args.ai),
        'input_set': 'ON-only (P1.5 ruling)',
        'transform': {'sx': params[0], 'sy': params[1],
                      'px0': params[2], 'py0': params[3],
                      'pdf_rect': [round(white_rect[0], 2), round(white_rect[1], 2),
                                   round(white_rect[2], 2), round(white_rect[3], 2)]},
        'stats': {
            'fill_centerlines': len(fill_segs),
            'fill_nonquad_skipped': skipped_fill,
            'stroke_segments': len(stroke_segs),
            'stroke_dup_dropped': n_dropped,
            'merged_runs': len(merged),
            'runs_out_of_sheet_dropped': n_outsheet,
            'final_wall_segments': len(walls_pdf),
            'door_gaps': len(doors),
        },
        'wall_thickness_pdf_units': {
            'median': round(med_thick, 2),
            'p10': round(thick_samples[int(len(thick_samples) * 0.1)], 2)
                   if len(thick_samples) >= 10 else None,
            'p90': round(thick_samples[min(len(thick_samples) - 1,
                                           int(len(thick_samples) * 0.9))], 2)
                   if thick_samples else None,
        },
        'doors_pdf': doors,
        'min_door_half_width_pdf_units': min_door_half,
        'provisional_min_spine_margin_px': margin,
        'regression_vs_hand': reg,
        'note': 'pixel artifacts use the coarse P2.0 transform; re-run with '
                '--transform after P3 calibration',
    }
    with open(os.path.join(args.out_dir, 'walls_pdf.json'), 'w', encoding='utf-8') as f:
        json.dump({'space': 'pdf', 'segments': segs_to_pairs(walls_pdf)},
                  f, indent=1)
    with open(os.path.join(args.out_dir, 'walls_px_draft.json'), 'w', encoding='utf-8') as f:
        json.dump({'space': 'pixels(%s, coarse)' % base_name,
                   'segments': segs_to_pairs(walls_px)}, f, indent=1)
    with open(os.path.join(args.out_dir, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    ov = {c['place_id']: {'old': c['old'], 'new': c['new']}
          for c in cands if 'new' in c}
    with open(os.path.join(args.out_dir, 'route_override_candidates.json'),
              'w', encoding='utf-8') as f:
        json.dump({'floor': args.floor, 'basis': 'coarse transform -- refresh after P3',
                   'candidates': ov, 'flagged': flagged}, f, ensure_ascii=False, indent=1)
    print('outputs in', args.out_dir)


if __name__ == '__main__':
    main()


