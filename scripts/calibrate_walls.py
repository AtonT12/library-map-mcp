r"""P3 calibration: refine the PDF-space -> base-JPG-pixel mapping.

Per floor:
  1. Sub-pixel sheet-edge localization around the P2.0 coarse bbox estimates.
  2. Base diagonal-affine transform (PDF y flipped), derived from sheet corners.
  3. Anchor fits where hand-drawn walls exist (5F): similarity (4 DoF) and
     general affine (6 DoF) against projected correspondences, Huber-reweighted
     (robust reweighting loop); selection prefers the most constrained model
     whenever its p95 residual is within 25% of the best (plan rule: take the
     simpler model when both perform comparably).
  4. Alignment polish on the diagonal model: coarse-to-fine grid search over
     translation and per-axis scale, maximizing the dark-pixel alignment score.
  5. Gate inputs (plan section 2.3): door half-widths converted to pixels
     (T-junction "seam" gaps excluded from the minimum), post-extraction
     minimum spine-to-wall spacing, residual p50/p95 (anchor fit when present,
     Monte-Carlo edge-uncertainty propagation otherwise).
  6. Convergence gate: 2 * p95 <= min(min_door_half_px - 6, min_spine_gap - 20).
     Over limit => FAIL: nothing is merged; findings are reported for P4.

Outputs (--out-dir, next to the P2 artifacts):
  calib_report.json                 full P3 record
  walls_px_final.json                extracted walls in calibrated pixel space
  route_override_candidates_calibrated.json  refreshed shelf-move proposals
  debug_final.png                    base + final walls + hand walls + doors

Usage:
  python calibrate_walls.py --floor 5F \
      --base ..\maps\5f_base.jpg --corridors ..\corridors.json \
      --csv ..\locations.csv --out-dir ..\extract_out\5F
"""
import argparse
import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_walls import (  # noqa: E402
    dist_pt_seg, dist, regression_vs_hand, min_spine_margin,
    draw_debug, shelf_candidates, WALL_CLEARANCE_PX, SPINE_MIN_MARGIN_PX,
)
from p15_wall_draft_score import bright_bbox  # noqa: E402


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _gauss_solve(a, b):
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            raise ValueError('singular matrix')
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col]
                for j in range(col, n + 1):
                    m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


# ---------------------------------------------------------------------------
# Models: ('diag', (sx, sy, px0, py0)) maps x -> px0 + (x-rx0)*sx,
#          y -> py0 + (ry1-y)*sy (y flip baked in via ry1 at call site)
#          ('sim', (cr, ci, tx, ty)) complex affine z' = c*z + t
#          ('aff', (a,b,c,d,e,f))    x'=a*x+b*y+c, y'=d*x+e*y+f
# ---------------------------------------------------------------------------

def make_diag(sx, sy, px0, py0, rx0, ry0, ry1):
    def t(x, y):
        return (px0 + (x - rx0) * sx, py0 + (ry1 - y) * sy)
    t.params = (sx, sy, px0, py0)
    t.rx0, t.ry0, t.ry1 = rx0, ry0, ry1
    return t


def diag_from_edges(edges, rx0, ry0, rx1, ry1):
    sx = (edges['right']['pos'] - edges['left']['pos']) / (rx1 - rx0)
    sy = (edges['bottom']['pos'] - edges['top']['pos']) / (ry1 - ry0)
    return sx, sy, edges['left']['pos'], edges['top']['pos']


def predict(kind, p, x, y, rx0, ry1):
    if kind == 'sim':
        cr, ci, tx, ty = p
        return (cr * x - ci * y + tx, ci * x + cr * y + ty)
    a, b, c, d, e, f = p
    return (a * x + b * y + c, d * x + e * y + f)


def eff_scales(kind, p):
    """Approximate per-axis pixel scales (for door-width conversion)."""
    if kind == 'sim':
        s = math.hypot(p[0], p[1])
        return s, s
    return math.hypot(p[0], p[1]), math.hypot(p[3], p[4])


# Pairs are (px_x, px_y, pdf_x, pdf_y): the models map PDF -> pixel, so the
# regression SOURCE is the PDF half (indices 2,3) and the TARGET the pixel
# half (indices 0,1).

def fit_sim(pairs, w=None):
    """Closed-form weighted similarity fit pix' = c*pdf + t (c complex)."""
    n = len(pairs)
    ws = w or [1.0] * n
    sw = sum(ws)
    zx = sum(ws[i] * pairs[i][2] for i in range(n)) / sw
    zy = sum(ws[i] * pairs[i][3] for i in range(n)) / sw
    ux = sum(ws[i] * pairs[i][0] for i in range(n)) / sw
    uy = sum(ws[i] * pairs[i][1] for i in range(n)) / sw
    sxx = syy = cru = cri = 0.0
    for i in range(n):
        dx, dy = pairs[i][2] - zx, pairs[i][3] - zy
        du, dv = pairs[i][0] - ux, pairs[i][1] - uy
        sxx += ws[i] * dx * dx
        syy += ws[i] * dy * dy
        cru += ws[i] * (du * dx + dv * dy)
        cri += ws[i] * (dv * dx - du * dy)
    denom = sxx + syy
    if denom <= 0:
        return None
    cr = cru / denom
    ci = cri / denom
    tx = ux - cr * zx + ci * zy
    ty = uy - ci * zx - cr * zy
    return (cr, ci, tx, ty)


def fit_affine(pairs, w=None):
    """Weighted LSQ per output axis over PDF features (x, y, 1)."""
    n = len(pairs)
    ws = w or [1.0] * n
    acc = [[0.0] * 3 for _ in range(3)]
    rb = [0.0, 0.0, 0.0]
    rv = [0.0, 0.0, 0.0]
    for i in range(n):
        fx = [pairs[i][2], pairs[i][3], 1.0]
        for jj in range(3):
            feat_j = fx[jj] * ws[i]
            rb[jj] += feat_j * pairs[i][0]
            rv[jj] += feat_j * pairs[i][1]
            for kk in range(3):
                acc[jj][kk] += feat_j * fx[kk]
    try:
        solx = _gauss_solve(acc, rb)
        soly = _gauss_solve(acc, rv)
    except ValueError:
        return None
    return tuple(solx) + tuple(soly)


def huber_weights(resid, k=1.5):
    if not resid:
        return []
    med = statistics.median(resid)
    mad = statistics.median([abs(r - med) for r in resid])
    sigma = 1.4826 * mad or 1.0
    out = []
    for r in resid:
        t = abs(r - med) / sigma
        out.append(1.0 if t <= k else (k / t) ** 2)
    return out


def predict_resid(kind, params, pair, rx0, ry1):
    hx, hy, dx, dy = pair
    if kind == 'diag':
        sx, sy, px0, py0 = params
        gx, gy = (px0 + (dx - rx0) * sx, py0 + (ry1 - dy) * sy)
    else:
        gx, gy = predict(kind, params, dx, dy, rx0, ry1)
    return math.hypot(gx - hx, gy - hy)


def robust_fit(fitter, kind, pairs, rx0, ry1, iterations=4):
    """Huber-reweighted iteration (robust RANSAC-style down-weighting)."""
    w = [1.0] * len(pairs)
    params = None
    resid = []
    for _ in range(iterations):
        cand = fitter(pairs, w)
        if cand is None:
            break
        params = cand
        if kind == 'diag':
            continue
        resid = [predict_resid(kind, params, pi, rx0, ry1) for pi in pairs]
        w = huber_weights(resid) or w
    return params, resid


def percentiles(values, ps=(50, 95)):
    vs = sorted(values)
    if not vs:
        return {}
    out = {}
    for p in ps:
        idx = min(len(vs) - 1, int(round((p / 100.0) * (len(vs) - 1))))
        out['p%d' % p] = round(vs[idx], 2)
    return out


# ---------------------------------------------------------------------------
# Sheet edge localization
# ---------------------------------------------------------------------------

def localize_edge(gray, est, axis, win=32, band=0.2):
    """Sub-pixel sheet edge position from a band-mean brightness profile.

    axis 'v' scans columns (left/right edges), 'h' scans rows (top/bottom).
    Returns (position, sigma, method). When no brightness step is visible in
    the window, falls back to the coarse estimate (sigma 4.0 = P2.0 quarter
    downscale quantization); edges at the image frame are exact by construction.
    """
    px = gray.load()
    W, H = gray.size
    size = W if axis == 'v' else H
    lo = max(0, int(est - win))
    hi = min(size, int(est + 1) + win)
    vals = []
    for pos in range(lo, hi):
        tot = cnt = 0
        if axis == 'v':
            ya, yb = int(H * band), H - int(H * band)
            step = max(1, (yb - ya) // 140)
            for yy in range(ya, yb, step):
                tot += px[pos, yy]
                cnt += 1
        else:
            xa, xb = int(W * band), W - int(W * band)
            step = max(1, (xb - xa) // 140)
            for xx in range(xa, xb, step):
                tot += px[xx, pos]
                cnt += 1
        vals.append(tot / max(1, cnt))
    best_i, best_d = None, 0.0
    for i in range(1, len(vals) - 1):
        d = min(abs(vals[i] - vals[i - 1]), abs(vals[i + 1] - vals[i]))
        if d > best_d:
            best_d, best_i = d, i
    if best_i is None or best_d < 4.0:
        pos = float(est)
        method = 'coarse'
        sigma = 4.0
        if pos <= 1.0:
            pos = 0.0
            method = 'frame'
            sigma = 1.0
        elif float(size - 1) - pos <= 1.0:
            pos = float(size - 1)
            method = 'frame'
            sigma = 1.0
        return pos, sigma, method
    l, c, r = vals[best_i - 1], vals[best_i], vals[best_i + 1]
    den = l - 2 * c + r
    sub = 0.5 * (l - r) / den if abs(den) > 1e-9 else 0.0
    pos = lo + best_i + max(-0.5, min(0.5, sub))
    sigma = 0.7 if best_d > 30 else (1.5 if best_d > 8 else 3.0)
    return pos, sigma, 'step'


def refine_sheet_edges(base_path, coarse_bb):
    from PIL import Image
    gray = Image.open(base_path).convert('L')
    cx, cy, cx1, cy1 = coarse_bb[:4]
    out = {'image_size': list(gray.size)}
    for name, est, axis in (('left', cx, 'v'), ('right', cx1, 'v'),
                            ('top', cy, 'h'), ('bottom', cy1, 'h')):
        pos, sigma, method = localize_edge(gray, est, axis)
        out[name] = {'pos': round(pos, 2), 'sigma': sigma, 'method': method}
    return out


# ---------------------------------------------------------------------------
# Hand-wall anchors (5F only has hand-drawn walls)
# ---------------------------------------------------------------------------

def seg_angle(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi


def seg_overlap_fraction(pa, pb, qa, qb):
    ax0, ax1 = sorted((pa[0], pb[0]))
    qx0, qx1 = sorted((qa[0], qb[0]))
    ay0, ay1 = sorted((pa[1], pb[1]))
    qy0, qy1 = sorted((qa[1], qb[1]))
    lh = max(abs(pb[0] - pa[0]), abs(qb[0] - qa[0]))
    lv = max(abs(pb[1] - pa[1]), abs(qb[1] - qa[1]))
    if abs(pb[0] - pa[0]) >= abs(pb[1] - pa[1]):
        inter = max(0.0, min(ax1, qx1) - max(ax0, qx0))
        span = max(lh, 1e-6)
    else:
        inter = max(0.0, min(ay1, qy1) - max(ay0, qy0))
        span = max(lv, 1e-6)
    return inter / span


def build_anchor_pairs(hand_walls, walls_pdf, tfunc, max_dist=600.0,
                       min_overlap=0.2, sample_n=10, one_to_one=False):
    """Match hand-drawn walls to extracted segments under the current
    transform; return ((hand_pixel_pt, pdf_projected_pt), ...) and per-hand
    wall match diagnostics. With one_to_one, each extracted segment is
    assigned to at most one hand wall (global greedy by match score)."""
    sx, sy, px0, py0 = tfunc.params
    rx0, ry0, ry1 = tfunc.rx0, tfunc.ry0, tfunc.ry1

    def inv(px, py):
        return (rx0 + (px - px0) / sx, ry1 - (py - py0) / sy)

    walls_px = [(tfunc(*a), tfunc(*b)) for a, b in walls_pdf]
    pairs = []
    matched = []
    cands = []
    too_short = set()
    has_cand = set()
    hand_pts = []
    for hi, w in enumerate(hand_walls):
        hax, hay = float(w[0][0]), float(w[0][1])
        hbx, hby = float(w[1][0]), float(w[1][1])
        hand_pts.append((hax, hay, hbx, hby))
        if dist(hax, hay, hbx, hby) < 40:
            too_short.add(hi)
            continue
        ang_h = seg_angle((hax, hay), (hbx, hby))
        for si, (ea, eb) in enumerate(walls_px):
            dang = abs(seg_angle(ea, eb) - ang_h)
            if dang > 0.35:
                continue
            d_mid = dist_pt_seg((hax + hbx) / 2, (hay + hby) / 2, ea, eb)
            if d_mid > max_dist:
                continue
            ov = seg_overlap_fraction((hax, hay), (hbx, hby), ea, eb)
            if ov < min_overlap:
                continue
            cands.append((d_mid + 40 * dang, hi, si, ea, eb, ov, d_mid))
            has_cand.add(hi)
    assign = {}
    if one_to_one:
        used = set()
        for score, hi, si, ea, eb, ov, d_mid in sorted(cands):
            if hi in assign or si in used:
                continue
            assign[hi] = (si, ea, eb, ov, d_mid)
            used.add(si)
    else:
        for score, hi, si, ea, eb, ov, d_mid in cands:
            if hi not in assign or score < assign[hi][0]:
                assign[hi] = (score, si, ea, eb, ov, d_mid)
        assign = {hi: v[1:] for hi, v in assign.items()}
    for hi, (hax, hay, hbx, hby) in enumerate(hand_pts):
        if hi in too_short:
            matched.append({'hand_wall': hi, 'matched': False,
                            'reason': 'too short'})
            continue
        if hi not in assign:
            reason = 'no candidate'
            if hi in has_cand and one_to_one:
                reason = 'no candidate (best seg taken one-to-one)'
            matched.append({'hand_wall': hi, 'matched': False,
                            'reason': reason})
            continue
        si, ea, eb, ov, d_mid = assign[hi]
        matched.append({'hand_wall': hi, 'matched': True, 'seg_index': si,
                        'overlap': round(ov, 2), 'gap_px': round(d_mid, 1)})
        vx, vy = eb[0] - ea[0], eb[1] - ea[1]
        l2 = vx * vx + vy * vy or 1.0
        for k in range(sample_n):
            f = k / max(1, sample_n - 1)
            X = hax + (hbx - hax) * f
            Y = hay + (hby - hay) * f
            u = max(0.0, min(1.0,
                             ((X - ea[0]) * vx + (Y - ea[1]) * vy) / l2))
            pdx, pdy = inv(ea[0] + u * vx, ea[1] + u * vy)
            pairs.append((X, Y, pdx, pdy))
    return pairs, matched


def anchor_residuals(pairs, kind, params, rx0, ry1):
    res = [predict_resid(kind, params, p, rx0, ry1) for p in pairs]
    st = percentiles(res)
    st['mean'] = round(statistics.fmean(res), 2) if res else None
    st['n_pairs'] = len(res)
    return st


# ---------------------------------------------------------------------------
# Alignment scoring and polish
# ---------------------------------------------------------------------------

def make_align_scorer(gray):
    """Dark-pixel alignment scorer with a precomputed MinFilter(9) grid
    (same metric as P1.5 score_segments). Returns fn(segs_pdf, tfunc)."""
    from PIL import ImageFilter
    dark = gray.filter(ImageFilter.MinFilter(9))
    dp = dark.load()
    dw, dh = dark.size
    lo = 150.0

    def scorer(segs_pdf, tfunc):
        hits = total = oob = 0
        for a, b in segs_pdf:
            pa, pb = tfunc(*a), tfunc(*b)
            L = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
            n = int(min(200, max(2, L / 12)))
            for k in range(n):
                f = k / max(1, n - 1)
                x = int(pa[0] + (pb[0] - pa[0]) * f)
                y = int(pa[1] + (pb[1] - pa[1]) * f)
                if x < 0 or y < 0 or x >= dw or y >= dh:
                    oob += 1
                    continue
                total += 1
                if dp[x, y] <= lo:
                    hits += 1
        return {'samples': total, 'dark_hits': hits, 'oob': oob,
                'score': round(hits / total, 4) if total else 0.0}
    return scorer


def polish_translation(segs_pdf, tfunc, scorer,
                       levels=((160, 16), (64, 8), (16, 4), (4, 1))):
    """Coarse-to-fine translation search maximizing the dark-pixel score."""
    cur = tfunc
    for rad, step in levels:
        sx, sy, px0, py0 = cur.params
        origin = (px0, py0)
        best_gain, best_off = 0.0, (0, 0)
        base = scorer(segs_pdf, cur)['score']
        for dx in range(-rad, rad + 1, step):
            for dy in range(-rad, rad + 1, step):
                if dx == 0 and dy == 0:
                    continue
                cand = make_diag(sx, sy, origin[0] + dx, origin[1] + dy,
                                 cur.rx0, cur.ry0, cur.ry1)
                s = scorer(segs_pdf, cand)['score']
                if s - base > best_gain + 1e-12:
                    best_gain, best_off = s - base, (dx, dy)
        if best_gain <= 0:
            break
        cur = make_diag(sx, sy, origin[0] + best_off[0],
                        origin[1] + best_off[1], cur.rx0, cur.ry0, cur.ry1)
    return cur


def polish_scale(segs_pdf, tfunc, scorer, rel_lo=-0.01, rel_hi=0.01, steps=13):
    sx, sy, px0, py0 = tfunc.params
    base = scorer(segs_pdf, tfunc)['score']
    best, best_s = None, base
    for i in range(steps):
        fx = 1.0 + (rel_hi - rel_lo) * i / (steps - 1)
        for j in range(steps):
            fy = 1.0 + (rel_hi - rel_lo) * j / (steps - 1)
            cand = make_diag(sx * fx, sy * fy, px0, py0,
                             tfunc.rx0, tfunc.ry0, tfunc.ry1)
            s = scorer(segs_pdf, cand)['score']
            if s > best_s + 1e-12:
                best, best_s = cand, s
    return best if best else tfunc


# ---------------------------------------------------------------------------
# Gate helpers
# ---------------------------------------------------------------------------

def mc_drift_p95(edges_info, rx0, ry0, rx1, ry1, samples=1500, seed=7):
    """Monte-Carlo propagation of sheet-edge jitter into pixel drift at
    representative content points (corners, edge midpoints, center)."""
    rng = random.Random(seed)
    sig = {s: edges_info[s]['sigma'] for s in ('left', 'top', 'right', 'bottom')}
    bs, bsy, bl, bt = diag_from_edges(edges_info, rx0, ry0, rx1, ry1)

    def base_t(x, y):
        return (bl + (x - rx0) * bs, bt + (ry1 - y) * bsy)

    pts = [(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1),
           ((rx0 + rx1) / 2, ry0), (rx1, (ry0 + ry1) / 2),
           ((rx0 + rx1) / 2, (ry0 + ry1) / 2), (rx0, (ry0 + ry1) / 2)]
    drifts = []
    for _ in range(samples):
        L = edges_info['left']['pos'] + rng.gauss(0, sig['left'])
        R = edges_info['right']['pos'] + rng.gauss(0, sig['right'])
        T = edges_info['top']['pos'] + rng.gauss(0, sig['top'])
        B = edges_info['bottom']['pos'] + rng.gauss(0, sig['bottom'])
        if L > R or T > B:
            continue
        t = make_diag((R - L) / (rx1 - rx0), (B - T) / (ry1 - ry0),
                      L, T, rx0, ry0, ry1)
        dmax = max(math.hypot(t(x, y)[0] - base_t(x, y)[0],
                              t(x, y)[1] - base_t(x, y)[1]) for x, y in pts)
        drifts.append(dmax)
    drifts.sort()
    if not drifts:
        return {'p50_px': None, 'p95_px': None, 'samples': 0}
    i95 = min(len(drifts) - 1, int(0.95 * (len(drifts) - 1)))
    i50 = min(len(drifts) - 1, int(0.5 * (len(drifts) - 1)))
    return {'p50_px': round(drifts[i50], 2), 'p95_px': round(drifts[i95], 2),
            'samples': len(drifts)}


def door_seam_flags(doors, walls_pdf, tol=4.0):
    """Gaps sitting at a perpendicular run end are T-junction layout seams,
    not passable doorways."""
    flagged = []
    for d in doors:
        cx, cy = d['position']
        horiz = abs(d['theta_deg']) < 45
        hit = False
        for a, b in walls_pdf:
            for p, other in ((a, b), (b, a)):
                if dist(p[0], p[1], cx, cy) > tol:
                    continue
                va, vb = other[0] - p[0], other[1] - p[1]
                ol = math.hypot(va, vb)
                if ol < 0.5:
                    continue
                perp = abs(va / ol) if horiz else abs(vb / ol)
                if perp <= 0.35:
                    hit = True
        flagged.append(hit)
    return flagged


def spine_contacts(corridors, fl, walls_px, top=3):
    fdata = corridors.get(fl) or {}
    wp = fdata.get('waypoints') or {}
    contacts = []
    for e in (fdata.get('edges') or []):
        a, b = wp.get(e[0]), wp.get(e[1])
        if not a or not b:
            continue
        L = dist(a[0], a[1], b[0], b[1])
        n = max(2, int(L / 40))
        for k in range(n + 1):
            f = k / n
            X = a[0] + (b[0] - a[0]) * f
            Y = a[1] + (b[1] - a[1]) * f
            best = None
            for wi, (p, q) in enumerate(walls_px):
                d = dist_pt_seg(X, Y, p, q)
                if best is None or d < best[0]:
                    best = (d, wi)
            if best is not None:
                contacts.append((best[0], X, Y, best[1]))
    contacts.sort()
    return [{'dist_px': round(c[0], 1), 'at': [round(c[1], 1), round(c[2], 1)],
             'wall_index': c[3]} for c in contacts[:top]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--floor', required=True)
    ap.add_argument('--base', required=True)
    ap.add_argument('--corridors', required=True)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--anchor-max-dist', type=float, default=600.0,
                    help='B1 diagnostic: tighten hand-to-extracted match radius')
    ap.add_argument('--anchor-min-overlap', type=float, default=0.2,
                    help='B1 diagnostic: minimum collinear overlap fraction')
    ap.add_argument('--anchor-one-to-one', action='store_true',
                    help='B1 diagnostic: each extracted seg matches at most '
                         'one hand wall (global greedy)')
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    out_dir = args.out_dir
    rep = load_json(os.path.join(out_dir, 'report.json'))
    walls_pdf = load_json(os.path.join(out_dir, 'walls_pdf.json'))['segments']
    corridors = load_json(args.corridors)
    fl = args.floor
    hand_walls = (corridors.get(fl) or {}).get('walls') or []

    tr = rep['transform']
    rx0, ry0, rx1, ry1 = [float(v) for v in tr['pdf_rect']]
    bb = bright_bbox(args.base)
    coarse_bb, base_im = bb[:4], bb[4]
    gray = base_im.convert('L')
    W, H = gray.size

    print('== P3 calibration:', fl)
    print('image %dx%d | coarse P2.0 sx=%.4f sy=%.4f px0=%.1f py0=%.1f'
          % (W, H, tr['sx'], tr['sy'], tr['px0'], tr['py0']))

    edges = refine_sheet_edges(args.base, coarse_bb)
    print('sheet edges:')
    for side in ('left', 'top', 'right', 'bottom'):
        e = edges[side]
        print('  %-7s pos=%8.1f sigma=%.1f (%s)'
              % (side, e['pos'], e['sigma'], e['method']))

    bs, bsy, bl, bt = diag_from_edges(edges, rx0, ry0, rx1, ry1)
    t_base = make_diag(bs, bsy, bl, bt, rx0, ry0, ry1)
    print('base diag transform: sx=%.4f sy=%.4f px0=%.1f py0=%.1f'
          % (bs, bsy, bl, bt))
    scorer = make_align_scorer(gray)
    s_coarse = scorer(walls_pdf, make_diag(tr['sx'], tr['sy'],
                                           tr['px0'], tr['py0'],
                                           rx0, ry0, ry1))['score']
    s_base = scorer(walls_pdf, t_base)['score']
    print('alignment score: coarse %.4f -> base-diag %.4f' % (s_coarse, s_base))

    # --- anchor fits ---------------------------------------------------------
    chosen_kind = 'diag'
    chosen_params = t_base.params
    chosen_t = t_base
    anchor_block = None
    pairs = []
    mc = mc_drift_p95(edges, rx0, ry0, rx1, ry1)
    if hand_walls:
        pairs, matched = build_anchor_pairs(
            hand_walls, walls_pdf, t_base,
            max_dist=args.anchor_max_dist,
            min_overlap=args.anchor_min_overlap,
            one_to_one=args.anchor_one_to_one)
        n_match = sum(1 for m in matched if m['matched'])
        print('anchors: %d/%d hand walls matched, %d point pairs'
              % (n_match, len(matched), len(pairs)))
        for m in matched:
            status = (('seg %d gap %.0fpx ov %.2f'
                       % (m.get('seg_index', -1), m.get('gap_px', -1),
                          m.get('overlap', 0))) if m['matched']
                      else 'UNMATCHED (%s)' % m.get('reason', 'no candidate'))
            print('  hand wall %s: %s' % (m['hand_wall'], status))
        candidates = {
            'diag': anchor_residuals(pairs, 'diag', chosen_params, rx0, ry1)}
        fit_params = {}
        if len(pairs) >= 12:
            sp, sr = robust_fit(fit_sim, 'sim', pairs, rx0, ry1)
            ap_, ar = robust_fit(fit_affine, 'aff', pairs, rx0, ry1)
            fit_params['sim'] = sp
            fit_params['aff'] = ap_
            for nm, prm in (('sim', sp), ('aff', ap_)):
                if prm is None:
                    continue
                res = [predict_resid(nm, prm, pi, rx0, ry1) for pi in pairs]
                st = percentiles(res)
                st['mean'] = round(statistics.fmean(res), 2)
                st['n_pairs'] = len(res)
                candidates[nm] = st
                print('%s residuals: %s' % (nm, st))
        p95s = {k: v['p95'] for k, v in candidates.items()}
        best_p95 = min(p95s.values())
        winner = 'diag'
        for nm in ('diag', 'sim', 'aff'):  # prefer most constrained
            if nm not in p95s:
                continue
            if p95s[nm] <= 1.25 * best_p95:
                winner = nm
                break
        if winner == 'sim':
            chosen_kind, chosen_params = 'sim', fit_params['sim']
            chosen_t = None
            cr, ci, tx, ty = fit_params['sim']
            print('similarity: scale=%.4f rotation=%.3f deg'
                  % (math.hypot(cr, ci), math.degrees(math.atan2(ci, cr))))
        elif winner == 'aff':
            chosen_kind, chosen_params = 'aff', fit_params['aff']
            chosen_t = None
            a, b, c_, d, e, f = fit_params['aff']
            print('affine: shear~%.3f deg | scales ~%.4f / %.4f'
                  % (math.degrees(math.atan2(b, a)),
                     math.hypot(a, b), math.hypot(d, e)))
        anchor_block = {
            'hand_walls_total': len(hand_walls),
            'hand_walls_matched': n_match,
            'point_pairs': len(pairs),
            'matches': matched,
            'residuals': candidates,
            'selected': winner,
            'mc_edge_drift': mc,
        }
        print('chosen model: %s (p95 px: %s)'
              % (winner, ', '.join('%s=%s' % (k, v) for k, v in p95s.items())))

    anchor_p95 = None

    st_sel = anchor_block['residuals'].get(chosen_kind, {}) if anchor_block else {}
    anchor_p95 = st_sel.get('p95')

    def tfinal(x, y):
        return chosen_t(x, y) if chosen_t is not None \
            else predict(chosen_kind, chosen_params, x, y, rx0, ry1)

    s_final = scorer(walls_pdf, tfinal)['score']
    print('final alignment score (selected model): %.4f' % s_final)

    # --- dark-pixel polish: separate review artifact, never merged into the
    #     authoritative transform (the score can chase non-wall dark masses
    #     and drift the sheet out of the image frame) ---------------------------
    polished_block = None
    if chosen_t is not None:
        tp = polish_translation(walls_pdf, chosen_t, scorer)
        tp = polish_scale(walls_pdf, tp, scorer)
        sp_after = scorer(walls_pdf, tp)['score']
        corners = (tp(rx0, ry1), tp(rx1, ry1), tp(rx1, ry0), tp(rx0, ry0))
        plausible = all(-30 <= X <= W - 1 + 30 and -30 <= Y <= H - 1 + 30
                        for (X, Y) in corners)
        overlap_polished = regression_vs_hand(corridors, fl,
                                              [[list(round(v, 1)
                                                     for v in tp(*a)),
                                                list(round(v, 1)
                                                     for v in tp(*b))]
                                               for a, b in walls_pdf])
        polished_block = {
            'note': 'dark-pixel-score-optimized; for P4 visual review only',
            'params': {'kind': 'diag', 'sx': tp.params[0], 'sy': tp.params[1],
                       'px0': tp.params[2], 'py0': tp.params[3],
                       'pdf_rect': [rx0, ry0, rx1, ry1]},
            'align_score': {'before': s_final, 'after': sp_after},
            'sheet_stays_in_image': plausible,
            'overlap_vs_hand': overlap_polished,
        }
        print('dark-polish review variant: score %.4f -> %.4f, sheet-in-image=%s'
              % (s_final, sp_after, plausible))

    # --- final pixel walls ----------------------------------------------------

    walls_px_final = [[[round(tfinal(*a)[0], 1), round(tfinal(*a)[1], 1)],
                       [round(tfinal(*b)[0], 1), round(tfinal(*b)[1], 1)]]
                      for a, b in walls_pdf]

    overlap = regression_vs_hand(corridors, fl, walls_px_final)
    if overlap:
        coarse_reg = (rep.get('regression_vs_hand') or {})
        print('overlap vs hand walls: median-of-medians %.1f px '
              '(P2.0 coarse was %.1f px)'
              % (overlap['median_of_medians_px'],
                 coarse_reg.get('median_of_medians_px', -1)))

    min_gap = min_spine_margin(corridors, fl, walls_px_final)
    contacts = spine_contacts(corridors, fl, walls_px_final)
    print('min spine-to-wall gap: %s px' % min_gap)

    # --- doors -----------------------------------------------------------------
    doors = rep.get('doors_pdf') or []
    seam = door_seam_flags(doors, walls_pdf) if doors else []
    sx_eff, sy_eff = (chosen_params[0], chosen_params[1]) if chosen_t is not None \
        else eff_scales(chosen_kind, chosen_params)
    door_table = []
    for di, d in enumerate(doors):
        s = sx_eff if abs(d['theta_deg']) < 45 else sy_eff
        door_table.append(dict(d, half_width_px=round(d['half_width'] * s, 1),
                               t_junction_seam=bool(seam[di])))
    raw_min = min((t_['half_width_px'] for t_ in door_table), default=None)
    real = [t_['half_width_px'] for t_ in door_table if not t_['t_junction_seam']]
    n_seam = sum(1 for f_ in seam if f_)
    real_min = min(real) if real else raw_min
    door_note = None
    if not real and raw_min is not None:
        door_note = 'every gap flagged as T-junction seam; raw minimum used'
    print('door gaps=%d (T-junction seams excluded=%d) | min half-width '
          '%s px (raw %s px)'
          % (len(doors), n_seam,
             ('%.1f' % real_min) if real_min is not None else 'n/a',
             ('%.1f' % raw_min) if raw_min is not None else 'n/a'))

    # --- residual source & convergence gate -------------------------------------
    p95_used = mc['p95_px']
    resid_source = 'mc_edge_drift'
    if anchor_p95 is not None:
        p95_used = max(mc['p95_px'] or 0.0, anchor_p95)
        resid_source = 'max(final_anchor_p95, mc_edge_drift)'
    bound_door = (real_min - WALL_CLEARANCE_PX) if real_min is not None else None
    bound_spine = (min_gap - SPINE_MIN_MARGIN_PX) if min_gap is not None else None
    limits = [b for b in (bound_door, bound_spine) if b is not None]
    limit = min(limits) if limits else None
    two_p95 = 2 * p95_used if p95_used is not None else None
    verdict = None
    if two_p95 is not None and limit is not None:
        verdict = 'PASS' if two_p95 <= limit else 'FAIL'
    gate = {
        'formula': '2*p95(residual) <= min(min_door_half_px-6, min_spine_gap-20)',
        'residual_p95_px': p95_used,
        'residual_source': resid_source,
        'two_p95_px': round(two_p95, 2) if two_p95 is not None else None,
        'min_door_half_px_raw': raw_min,
        'min_door_half_px_excl_seams': real_min,
        't_junction_seams_excluded': n_seam,
        'door_note': door_note,
        'bound_door_px': round(bound_door, 2) if bound_door is not None else None,
        'min_spine_gap_px': min_gap,
        'bound_spine_px': round(bound_spine, 2) if bound_spine is not None else None,
        'gate_limit_px': round(limit, 2) if limit is not None else None,
        'verdict': verdict,
        'spine_contacts_top3': contacts,
    }
    print('GATE: 2*p95=%.1f vs limit %.1f -> %s'
          % (two_p95, limit, verdict)
          if two_p95 is not None and limit is not None
          else 'GATE: incomplete inputs')

    # --- shelf move proposals under the calibrated transform -------------------
    from PIL import Image as _PImage
    gray_im = _PImage.open(args.base).convert('L')
    base_name = os.path.basename(args.base)
    cands, flagged = shelf_candidates(args.csv, fl, walls_px_final,
                                      gray_im, gray_im.size, base_name)
    ov = {c['place_id']: {'old': c['old'], 'new': c['new']}
          for c in cands if 'new' in c}
    with open(os.path.join(out_dir, 'route_override_candidates_calibrated.json'),
              'w', encoding='utf-8') as f:
        json.dump({'floor': fl,
                   'basis': 'P3 calibrated transform (%s)' % chosen_kind,
                   'candidates': ov, 'flagged': flagged},
                  f, ensure_ascii=False, indent=1)
    print('shelf override candidates: %d (flagged %d)' % (len(ov), len(flagged)))

    # --- outputs ------------------------------------------------------------------
    with open(os.path.join(out_dir, 'walls_px_final.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'space': 'pixels(%s, P3 calibrated %s)'
                 % (base_name, chosen_kind),
                   'transform': ({'kind': chosen_kind,
                                  'params': [float(v) for v in chosen_params],
                                  'rx0': rx0, 'ry1': ry1}
                                 if chosen_t is None else
                                 {'kind': 'diag',
                                  'sx': chosen_params[0], 'sy': chosen_params[1],
                                  'px0': chosen_params[2], 'py0': chosen_params[3],
                                  'pdf_rect': [rx0, ry0, rx1, ry1]}),
                   'segments': walls_px_final}, f, indent=1)
    draw_debug(args.base, walls_px_final, doors, tfinal, hand_walls, [],
               os.path.join(out_dir, 'debug_final.png'))

    report = {
        'floor': fl,
        'phase': 'P3 calibration',
        'input_set': rep.get('input_set'),
        'image_size': [W, H],
        'sheet_edges': edges,
        'transforms': {
            'coarse_p20': tr,
            'base_diag': {'sx': bs, 'sy': bsy, 'px0': bl, 'py0': bt,
                          'pdf_rect': [rx0, ry0, rx1, ry1]},
            'final': ({'kind': chosen_kind,
                       'params': [float(v) for v in chosen_params],
                       'rx0': rx0, 'ry1': ry1}
                      if chosen_t is None else
                      {'kind': 'diag', 'sx': chosen_params[0],
                       'sy': chosen_params[1], 'px0': chosen_params[2],
                       'py0': chosen_params[3], 'pdf_rect': [rx0, ry0, rx1, ry1]}),
        },
        'alignment_score': {'coarse': s_coarse, 'base_diag': s_base,
                            'final': s_final},
        'dark_polish_review': polished_block,
        'anchors': anchor_block,
        'overlap_vs_hand': overlap,
        'min_spine_gap_px': min_gap,
        'spine_contacts_top3': contacts,
        'doors': door_table,
        'mc_edge_drift': mc,
        'gate': gate,
    }
    with open(os.path.join(out_dir, 'calib_report.json'), 'w',
              encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print('outputs in', out_dir)


if __name__ == '__main__':
    main()
