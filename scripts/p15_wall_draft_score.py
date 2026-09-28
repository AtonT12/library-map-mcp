"""P2.0 coarse transform + P1.5 visibility ruling (draft wall scoring).

Usage: python p15_wall_draft_score.py <file.ai> <base.jpg> [--json out.json]
Stdlib + Pillow only. Produces draft wall segments from candidate layer sets
(ON-only vs ON+OFF), maps them onto the base image via a coarse rectangle fit,
and scores darkness alignment per set.
"""
import re
import sys
import json
from collections import Counter

from ai_layer_inventory import (parse_xref_offsets, slice_objects, tokenize,
                                ref_list, unescape_pdf_string, decode_name,
                                mul, pt, NUM, ID)


def load_floor(path):
    data = open(path, 'rb').read()
    offs = parse_xref_offsets(data)
    root_xr = offs['__root__']
    objs, _ = slice_objects(data, offs)
    rh = objs[root_xr]['head']
    pages_m = re.search(r'/Pages\s+(\d+)\s+0\s+R', rh)
    pagenode = objs[int(pages_m.group(1))]['head']
    kids = re.findall(r'(\d+)\s+0\s+R',
                      re.search(r'/Kids\s*\[([^\]]*)\]', pagenode).group(1))
    page_xr = int(kids[0])
    ph = objs[page_xr]['head']
    mb = re.search(r'/MediaBox\s*\[([^\]]*)\]', ph)
    art = re.search(r'/ArtBox\s*\[([^\]]*)\]', ph)
    cm = re.search(r'/Contents\s+(\d+)\s+0\s+R', ph)
    content_stream = objs[int(cm.group(1))]['payload'] or b''
    props = {int(i): int(x) for i, x in
             re.findall(rb'/MC(\d+)\s+(\d+)\s+0\s+R',
                         data[offs[page_xr]:offs[page_xr] + 4096])}
    ocprops_i = rh.find('/OCProperties')
    opseg = data[offs[root_xr]:][ocprops_i:ocprops_i + 3000]
    ocgs_all = ref_list(opseg, '/OCGs')
    d_i = opseg.find(b'/D')
    on_default = ref_list(opseg[d_i:d_i + 3000], '/ON')
    off_default = ref_list(opseg[d_i:d_i + 3000], '/OFF')
    layers = []
    for idx, xr in enumerate(ocgs_all):
        o = data[offs[xr]:offs[xr] + 2048]
        eob = o.find(b'endobj')
        raw = o[:eob] if eob > 0 else o
        nm_m = re.search(rb'/Name\s*\(((?:\\.|[^\\()\n])*)\)', raw)
        name = '?'
        if nm_m:
            name, _ = decode_name(unescape_pdf_string(nm_m.group(1)))
        st = 'off' if xr in off_default else ('on-d' if xr in on_default else 'on')
        layers.append({'idx': idx, 'xref': xr, 'name': name, 'state': st})
    prop_mc_by_xr = {xr: mc for mc, xr in props.items()}
    for L in layers:
        L['mc'] = prop_mc_by_xr.get(L['xref'])
    fm_names = {}
    for k in offs:
        if isinstance(k, str):
            continue
        seg = data[offs[k]:offs[k] + 2048]
        for nm, xr in re.findall(rb'/(Fm\w+|Im\w+)\s+(\d+)\s+0\s+R', seg):
            fm_names.setdefault(nm.decode('latin1'), int(xr))
    forms = {}
    for n, xr in fm_names.items():
        ob = objs.get(xr)
        if not ob or ob['payload'] is None:
            continue
        mm = re.search(r'/Matrix\s*\[([^\]]*)\]', ob['head'])
        margs = [float(v) for v in mm.group(1).split()] if mm else []
        mform = tuple(margs[:6]) if len(margs) == 6 else ID
        forms[n] = {'tokens': tokenize(ob['payload']), 'matrix': mform}
    return {
        'layers': layers, 'props': props, 'content_tokens': tokenize(content_stream),
        'forms': forms, 'fm_names': fm_names,
        'media_box': [float(v) for v in mb.group(1).split()],
        'art_box': [float(v) for v in art.group(1).split()] if art else None,
        'objs': objs,
    }


WALL_FILL = (0.45, 0.43, 0.61)
WALL_STROKE = (0.49, 0.39, 0.61)
TOL = 0.07
FILL_COLORS = {b'rg': 3, b'g': 1, b'k': 3}
STROKE_COLORS = {b'RG': 3, b'G': 1, b'K': 3}


def close(a, b, tol=TOL):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def skip_to_emc(tokens, i):
    j = i + 1
    d = 1
    N = len(tokens)
    while j < N:
        t = tokens[j]
        if t in (b'BDC', b'BMC'):
            d += 1
        elif t == b'EMC':
            d -= 1
            if d == 0:
                return j + 1
        j += 1
    return N


class GeoWalker:
    """Collects wall-colored fill polys / stroke polylines with form expansion."""

    def __init__(self, floordata, allowed_states):
        self.fds = floordata
        self.allowed = set(allowed_states)
        self.forms = floordata['forms']
        mc_map = {L['mc']: L for L in floordata['layers'] if L['mc'] is not None}
        self.mc_layer = mc_map
        self.poly_fills = []
        self.line_strokes = []
        self.white_polys = []
        self.stats = Counter()
        self.ctx_layer = 'root'
        self.oc_depth = 0

    def run(self):
        toks = self.fds['content_tokens']
        self._walk(toks, ID, frozenset(), 0)

    def _emit_fill(self, color, poly, ctm):
        pts = [pt(x, y, ctm) for x, y in poly]
        xs0 = [p[0] for p in pts]
        ys0 = [p[1] for p in pts]
        if all(abs(c - 1.0) <= TOL for c in color[:3]):
            area = (max(xs0) - min(xs0)) * (max(ys0) - min(ys0))
            self.white_polys.append((area, (min(xs0), min(ys0), max(xs0), max(ys0))))
        if not close(color, WALL_FILL):
            self.stats['fill_other'] += 1
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if max(w, h) < 8:
            self.stats['fill_tiny'] += 1
            return
        self.poly_fills.append({'pts': pts, 'layer': self.ctx_layer})
        self.stats['fill_wall'] += 1

    def _walk(self, tokens, ctm_base, visited, depth):
        if depth > 6:
            return
        nums = []
        last_names = []
        ctm_stack = [ctm_base]
        cur_ctm = ctm_base
        subs = []
        poly = []
        fill_color = (0.0, 0.0, 0.0)
        stroke_color = (0.0, 0.0, 0.0)
        i = 0
        N = len(tokens)

        def take(k):
            out = nums[-k:] if len(nums) >= k else []
            del nums[-k:]
            return out

        while i < N:
            tok = tokens[i]
            if NUM.match(tok):
                nums.append(float(tok))
                i += 1
                continue
            if tok in (b'<<', b'>>') or tok.startswith(b'<'):
                nums = []
                i += 1
                continue
            if tok.startswith(b'/'):
                last_names.append(tok[1:])
                if len(last_names) > 3:
                    last_names.pop(0)
                nums = []
                i += 1
                continue
            if tok.startswith(b'(') or tok.startswith(b'['):
                nums = []
                i += 1
                continue
            kw = tok
            if kw in (b'BDC', b'BMC'):
                window = tokens[max(0, i - 8):i]
                is_oc = b'/OC' in window
                mc = None
                for t in window:
                    if isinstance(t, bytes) and t.startswith(b'/MC') and t[3:].isdigit():
                        mc = int(t[3:])
                        break
                L = self.mc_layer.get(mc)
                st = L['state'] if L is not None else 'root'
                if is_oc and st not in self.allowed:
                    i = skip_to_emc(tokens, i)
                    continue
                if is_oc:
                    self.oc_depth += 1
                    if L is not None:
                        self.ctx_layer = L['name']
                i += 1
                continue
            if kw == b'EMC':
                if self.oc_depth > 0:
                    self.oc_depth -= 1
                i += 1
                continue
            if kw == b'q':
                ctm_stack.append(cur_ctm)
            elif kw == b'Q':
                if len(ctm_stack) > 1:
                    ctm_stack.pop()
                cur_ctm = ctm_stack[-1]
            elif kw == b'cm':
                m6 = take(6)
                if len(m6) == 6:
                    cur_ctm = ctm_stack[-1] = mul(tuple(m6), cur_ctm)
            elif kw in (b'm', b'y'):
                if len(poly) >= 3:
                    subs.append(poly)
                elif poly:
                    self.stats['fill_nopoly'] += 1
                vals = take(2)
                poly = [(vals[0], vals[1])] if len(vals) == 2 else []
            elif kw in (b'l',):
                vals = take(2)
                if len(vals) == 2:
                    poly.append((vals[0], vals[1]))
            elif kw in (b'v', b'u'):
                vals = take(4)
                if len(vals) == 4:
                    poly.append((vals[2], vals[3]))
            elif kw == b'c':
                vals = take(6)
                if len(vals) == 6:
                    poly.append((vals[4], vals[5]))
            elif kw == b're':
                vals = take(4)
                if len(vals) == 4:
                    x, y, w, h = vals
                    if abs(w) > 1e-6 or abs(h) > 1e-6:
                        subs.append([(x, y), (x + w, y),
                                     (x + w, y + h), (x, y + h)])
            elif kw in (b'f', b'F', b'b', b'B', b'f*', b'b*'):
                batch = subs + ([poly] if poly else [])
                for sp in batch:
                    if len(sp) >= 3:
                        self._emit_fill(fill_color, sp, ctm_stack[-1])
                    else:
                        self.stats['fill_nopoly'] += 1
                subs = []
                poly = []
            elif kw in (b'S', b's'):
                pts = [p for sp in (subs + ([poly] if poly else [])) for p in sp]
                if len(pts) >= 2:
                    if close(stroke_color, WALL_STROKE):
                        self.line_strokes.append(
                            {'pts': [pt(*p, ctm_stack[-1]) for p in pts],
                             'layer': self.ctx_layer})
                        self.stats['stroke_wall'] += 1
                    else:
                        self.stats['stroke_other'] += 1
                subs = []
                poly = []
            elif kw == b'Do':
                m6 = take(6)
                marg = tuple(m6[:6]) if len(m6) == 6 else ID
                name = last_names[-1].decode('latin1') if last_names else ''
                nums = []
                fd = self.forms.get(name)
                if fd is not None and name not in visited:
                    fctm = mul(cur_ctm, mul(marg, fd['matrix']))
                    self._walk(fd['tokens'], fctm, visited | {name}, depth + 1)
            elif kw in FILL_COLORS:
                k = FILL_COLORS[kw]
                vals = take(k)
                if len(vals) == k:
                    fill_color = tuple(vals) if k == 3 else (vals[0],) * 3
            elif kw in STROKE_COLORS:
                k = STROKE_COLORS[kw]
                vals = take(k)
                if len(vals) == k:
                    stroke_color = tuple(vals) if k == 3 else (vals[0],) * 3
            elif kw in (b'Tf', b'BT', b'ET', b'gs'):
                pass
            i += 1


def segments_from_geom(gw):
    segs = []
    skipped = 0
    for rec in gw.poly_fills:
        poly = rec['pts'] if isinstance(rec, dict) else rec
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        w = xmax - xmin
        h = ymax - ymin
        if max(w, h) < 8:
            skipped += 1
            continue
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0
        if w >= h:
            segs.append(((xmin, cy), (xmax, cy)))
        else:
            segs.append(((cx, ymin), (cx, ymax)))
    for line in gw.line_strokes:
        pts = line['pts'] if isinstance(line, dict) else line
        for a, b in zip(pts[:-1], pts[1:]):
            if ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 > 8:
                segs.append((a, b))
    return segs, skipped


def bright_bbox(jpg_path, scale=4):
    from PIL import Image
    im = Image.open(jpg_path).convert('L')
    W, H = im.size
    im2 = im.resize((max(1, W // scale), max(1, H // scale)))
    px = im2.load()
    w2, h2 = im2.size
    rowc = [0] * h2
    colc = [0] * w2
    for y in range(h2):
        for x in range(w2):
            if px[x, y] >= 235:
                rowc[y] += 1
                colc[x] += 1
    rx_max = max(rowc)
    cx_max = max(colc)
    r_lo = next(y for y in range(h2) if rowc[y] >= 0.6 * rx_max)
    r_hi = next(y for y in range(h2 - 1, -1, -1) if rowc[y] >= 0.6 * rx_max)
    c_lo = next(x for x in range(w2) if colc[x] >= 0.6 * cx_max)
    c_hi = next(x for x in range(w2 - 1, -1, -1) if colc[x] >= 0.6 * cx_max)
    return (c_lo * scale, r_lo * scale, (c_hi + 1) * scale, (r_hi + 1) * scale, im)


def make_transform(pdf_rect, pix_bbox):
    rx0, ry0, rx1, ry1 = pdf_rect
    Px0, Py0, Px1, Py1 = pix_bbox[:4]
    sx = (Px1 - Px0) / (rx1 - rx0)
    sy = (Py1 - Py0) / (ry1 - ry0)

    def t(x, y):
        px = Px0 + (x - rx0) * sx
        py = Py0 + (ry1 - y) * sy
        return px, py

    return t, (sx, sy, Px0, Py0, rx0, ry0)


def score_segments(segs, tfunc, gray_im, lo=150.0):
    from PIL import ImageFilter
    dark = gray_im.filter(ImageFilter.MinFilter(9))
    dp = dark.load()
    dw, dh = dark.size
    hits = 0
    total = 0
    oob = 0
    for (ax, ay), (bx, by) in segs:
        L = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        n = int(min(200, max(2, L / 12)))
        for k in range(n):
            f = k / max(1, n - 1)
            X = ax + (bx - ax) * f
            Y = ay + (by - ay) * f
            x = int(X)
            y = int(Y)
            if x < 0 or y < 0 or x >= dw or y >= dh:
                oob += 1
                continue
            total += 1
            if dp[x, y] <= lo:
                hits += 1
    frac = hits / total if total else 0.0
    return {'samples': total, 'dark_hits': hits, 'oob': oob,
            'score': round(frac, 4)}


def debug_overlay(jpg_path, segs_pdf, tfunc, out_png, scale=0.2):
    from PIL import Image, ImageDraw
    im = Image.open(jpg_path).convert('RGB')
    W, H = im.size
    im2 = im.resize((int(W * scale), int(H * scale)))
    d = ImageDraw.Draw(im2)
    lw = max(1, int(3 * scale))
    for a, b in segs_pdf:
        ax, ay = tfunc(a[0], a[1])
        bx, by = tfunc(b[0], b[1])
        d.line([(ax * scale, ay * scale), (bx * scale, by * scale)], fill='red', width=lw)
    im2.save(out_png)
    return out_png


def local_dark_scores(segs_pdf, tfunc, im, win=96, step=8, lo=150.0):
    from PIL import ImageFilter
    dark = im.filter(ImageFilter.MinFilter(9))
    dp = dark.load()
    DW, DH = dark.size
    out = []
    for a, b in segs_pdf:
        pa, pb = tfunc(*a), tfunc(*b)
        L = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2) ** 0.5
        n = int(min(30, max(3, L / 14)))
        pts = [(pa[0] + (pb[0] - pa[0]) * k / max(1, n - 1),
                pa[1] + (pb[1] - pa[1]) * k / max(1, n - 1)) for k in range(n)]
        best_s, best_off = 0.0, (0, 0)
        for dx in range(-win, win + 1, step):
            for dy in range(-win, win + 1, step):
                h = tot = 0
                for X, Y in pts:
                    x = int(X + dx)
                    y = int(Y + dy)
                    if x < 0 or y < 0 or x >= DW or y >= DH:
                        continue
                    tot += 1
                    if dp[x, y] <= lo:
                        h += 1
                s = h / tot if tot else 0.0
                if s > best_s:
                    best_s, best_off = s, (dx, dy)
        out.append(best_s)
    return out


def draft_set(floordata, allowed_states):
    gw = GeoWalker(floordata, allowed_states)
    gw.run()
    segs, skipped = segments_from_geom(gw)
    white_rect = max((wp[1] for wp in gw.white_polys), key=lambda r: (r[2] - r[0]) * (r[3] - r[1]),
                     default=None)
    return segs, dict(gw.stats), skipped, white_rect


def score_mapped(segs_pdf, tfunc, im):
    mapped = [((tfunc(a[0], a[1])), (tfunc(b[0], b[1]))) for a, b in segs_pdf]
    return score_segments(mapped, tfunc, im)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    raw = sys.argv[1:]
    dbg_out = None
    if '--dbg' in raw:
        i = raw.index('--dbg')
        dbg_out = raw[i + 1]
        del raw[i:i + 2]
    ai_path, jpg_path = raw[0], raw[1]
    floordata = load_floor(ai_path)
    print('floor:', ai_path.split('\\')[-1])
    print('layer states:', dict(Counter(L['state'] for L in floordata['layers'])))
    for L in floordata['layers']:
        print('  mc=%s obj=%d %-4s %r' % (L['mc'], L['xref'], L['state'], L['name']))
    bb = bright_bbox(jpg_path)
    pix_bbox, im = bb[:4], bb[4]
    print('bright bbox (px):', pix_bbox[:4], '| base size:', im.size)
    segs_on, st_on, sk_on, _ = draft_set(floordata, ('on-d', 'on', 'root'))
    segs_all, st_all, sk_all, white_rect = draft_set(
        floordata, ('on-d', 'on', 'off', 'root'))
    for tag, triple in (('ON-only ', (segs_on, st_on, sk_on)),
                        ('ON+OFF  ', (segs_all, st_all, sk_all))):
        segs, st, sk = triple
        extra = {k: v for k, v in st.items() if k not in ('fill_wall', 'stroke_wall')}
        print('%s fills=%d strokes=%d segs=%d nonrect_skipped=%d other=%s' %
              (tag, st.get('fill_wall', 0), st.get('stroke_wall', 0), len(segs), sk, extra))
    mb = floordata['media_box']
    eps = 5.0
    if white_rect is not None and not (white_rect[0] >= mb[0] - eps
                                       and white_rect[1] >= mb[1] - eps
                                       and white_rect[2] <= mb[2] + eps
                                       and white_rect[3] <= mb[3] + eps):
        all_gw = GeoWalker(floordata, ('on-d', 'on', 'off', 'root'))
        all_gw.run()
        in_box = [wp[1] for wp in all_gw.white_polys
                  if wp[1][0] >= mb[0] - eps and wp[1][1] >= mb[1] - eps
                  and wp[1][2] <= mb[2] + eps and wp[1][3] <= mb[3] + eps]
        white_rect = max(in_box,
                         key=lambda r: (r[2] - r[0]) * (r[3] - r[1]),
                         default=None)
    if white_rect is None:
        white_rect = tuple(mb)
        print('no white background rect found; falling back to MediaBox')
    else:
        print('content white rect (pdf):', ['%.3f' % v for v in white_rect])
    t, params = make_transform(tuple(white_rect), pix_bbox)
    print('coarse transform: sx=%.3f sy=%.3f px/unit  (unif-scale residual %.2f%%)' %
          (params[0], params[1],
           abs(params[0] - params[1]) / max(params[0], params[1]) * 100))
    if dbg_out:
        print('debug overlay:', debug_overlay(jpg_path, segs_on, t, dbg_out))
    res_on = score_mapped(segs_on, t, im)
    res_all = score_mapped(segs_all, t, im)
    print('global score ON-only :', json.dumps(res_on))
    print('global score ON+OFF  :', json.dumps(res_all))
    import statistics as _st
    loc_on = local_dark_scores(segs_on, t, im)
    n = len(loc_on)
    med = _st.median(loc_on) if n else 0.0
    frac_ge_05 = sum(1 for s in loc_on if s >= 0.5) / n if n else 0.0
    print('local per-seg dark-rate ON-only: median=%.2f mean=%.2f frac>=0.5=%.2f' %
          (med, (sum(loc_on) / n) if n else 0.0, frac_ge_05))
    same_segs = (len(segs_on) == len(segs_all))
    chosen = 'ON-only'
    if res_all['score'] > res_on['score'] + 0.05:
        chosen = 'ON+OFF'
    local_pass = med >= 0.6 and frac_ge_05 >= 0.5
    verdict = {
        'chosen_set': chosen,
        'same_segment_set': same_segs,
        'global_scores': [res_on['score'], res_all['score']],
        'local_median_dark_rate': round(med, 3),
        'local_frac_ge_0.5': round(frac_ge_05, 3),
        'pass': local_pass,
        'note': ('OFF adds no distinct vector wall segments; minimal sufficient set wins.'
                 if same_segs else 'sets differ; higher dark-alignment score wins'),
    }
    print('VERDICT:', json.dumps(verdict, ensure_ascii=False))
    out = {'file': ai_path, 'pix_bbox': pix_bbox[:4], 'white_rect': list(white_rect),
           'transform_params': list(params), 'on': res_on, 'on_plus_off': res_all,
           'local_scores': [round(s, 3) for s in loc_on],
           'verdict': verdict}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
