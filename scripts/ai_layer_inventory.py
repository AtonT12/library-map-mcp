"""P1 layer inventory for .ai (PDF-1.6 container) floor plans.

Usage: python ai_layer_inventory.py <file.ai> [--json out.json]
Stdlib only. Emits a markdown report on stdout; optional JSON sidecar.
"""
import re
import sys
import json
import zlib
from collections import Counter

TOK = re.compile(rb"""
  (?P<array>\[(?:[^\\\[\]]|\\.)*\])
| (?P<hexstr><[0-9A-Fa-f\s]+>>)
| (?P<string>\((?:\\.|[^\\()\n])*\))
| (?P<dic_open><<)
| (?P<dic_close>>>)
| (?P<kw>[A-Za-z][A-Za-z0-9.+\-]*)
| (?P<num>-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
| (?P<name>/[!-~]+)
""", re.X)

NUM = re.compile(rb'^-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$')
PATH_COORD_OPS = {b'm': 2, b'y': 2, b'l': 2, b're': 4, b'c': 6, b'v': 4, b'u': 4}
FILL_OPS = {b'f', b'F', b'f*', b'b', b'B', b'b*', b'B*'}
STROKE_OPS = {b'S', b's'}
FILL_COLOR_OPS = {b'rg': 3, b'g': 1, b'k': 3}
STROKE_COLOR_OPS = {b'RG': 3, b'G': 1, b'K': 3}


def parse_xref_offsets(data):
    m = re.search(rb'startxref\s*(\d+)', data[-1024:])
    if not m:
        raise SystemExit('no startxref')
    pos = int(m.group(1))
    offs = {}
    guard = 0
    while guard < 50:
        guard += 1
        if data[pos:pos + 4] != b'xref':
            break
        p = pos + 4
        prev = None
        while True:
            j = p
            while j < len(data) and data[j:j + 1] in b' \r\n\t':
                j += 1
            ln_end = data.find(b'\n', j)
            if ln_end < 0:
                ln_end = len(data)
            parts = data[j:ln_end].split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                start, cnt = int(parts[0]), int(parts[1])
                q = ln_end + 1
                for i in range(cnt):
                    s = q
                    while s < len(data) and data[s:s + 1] in b' \r\n\t':
                        s += 1
                    e2 = data.find(b'\n', s)
                    if e2 < 0:
                        e2 = len(data) - 1
                    raw = data[s:e2].strip()
                    em = re.match(rb'^(\d{10})\s+\d+\s+([nf])', raw)
                    if em and em.group(2) == b'n':
                        offs[int(start + i)] = int(em.group(1))
                    q = e2 + 1
                p = q
                continue
            if parts and parts[0] == b'trailer':
                gt = data.find(b'>>', j)
                seg = data[j:gt]
                pm = re.search(rb'/Prev\s+(\d+)', seg)
                rm = re.search(rb'/Root\s+(\d+)\s+0\s+R', seg)
                if rm:
                    offs['__root__'] = int(rm.group(1))
                prev = int(pm.group(1)) if pm else None
                break
            break
        if prev is None:
            break
        pos = prev
    return offs


def slice_objects(data, offs):
    objs = {}
    for k in sorted(x for x in offs if not isinstance(x, str)):
        o = offs[k]
        sm = data.find(b'stream', o, o + 4096)
        eo = data.find(b'endobj', o)
        head_end = sm if 0 < sm < eo else eo
        head = data[o:head_end]
        payload = None
        if 0 < sm < eo:
            skm = re.search(rb'stream\r?\n', data[o:o + 4096])
            if skm:
                p0 = o + skm.end()
                es = data.find(b'endstream', p0)
                payload = data[p0:es]
        htxt = head.decode('latin1', 'replace')
        d = {'head': htxt, 'payload_raw': payload, 'payload': payload}
        if b'FlateDecode' in head and payload is not None:
            cands = set()
            lm = re.search(rb'/Length\s+(\d+)', head)
            if lm:
                n = int(lm.group(1))
                cands.add(n)
                cands.add(n - 1)
                cands.add(n + 1)
            for delta in (0, 1, 2):
                cands.add(len(payload) - delta)
            for cand in sorted(cands, reverse=True):
                if cand <= 0:
                    continue
                try:
                    d['payload'] = zlib.decompress(payload[:cand])
                    break
                except Exception:
                    pass
            else:
                d['payload'] = None
        objs[k] = d
    return objs, offs.get('__root__')


def unescape_pdf_string(raw):
    out = bytearray()
    i = 0
    esc = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12,
           ord('('): 40, ord(')'): 41, ord('\\'): 92}
    while i < len(raw):
        c = raw[i]
        if c == 0x5C and i + 1 < len(raw):
            n = raw[i + 1]
            if n in esc:
                out.append(esc[n])
                i += 2
                continue
            if 48 <= n <= 55:
                j = i + 1
                digits = b''
                while j < len(raw) and len(digits) < 3 and 48 <= raw[j] <= 55:
                    digits += bytes([raw[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(n)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def decode_name(b):
    if b[:2] in (b'\xfe\xff', b'\xff\xfe'):
        try:
            return b.decode('utf-16'), 'utf16-bom'
        except Exception:
            pass
    if b.startswith(b'\xfe\xff'):
        try:
            return b[2:].decode('utf-16-be'), 'utf16be-bom'
        except Exception:
            pass
    if b.startswith(b'\xff\xfe'):
        try:
            return b[2:].decode('utf-16-le'), 'utf16le-bom'
        except Exception:
            pass
    try:
        s = b.decode('utf-8')
        return s, ('ascii' if all(ord(ch) < 128 for ch in s) else 'utf8')
    except Exception:
        pass
    return b.decode('cp1252', 'replace'), 'cp1252?'


def tokenize(stream):
    out = []
    pos = 0
    L = len(stream)
    while pos < L:
        ch = stream[pos]
        if ch == 0x25:
            nl = stream.find(b'\n', pos)
            pos = L if nl < 0 else nl + 1
            continue
        m = TOK.match(stream, pos)
        if not m:
            pos += 1
            continue
        out.append(m.group(0))
        pos = m.end()
    return out


ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(M, N):
    a, b, c, d, e, f = M
    a2, b2, c2, d2, e2, f2 = N
    return (a * a2 + b * c2, a * b2 + b * d2,
            c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def pt(x, y, M):
    a, b, c, d, e, f = M
    return (a * x + c * y + e, b * x + d * y + f)


def poly_area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def new_frame(label):
    return {
        'label': label, 'ops': Counter(), 'widths': [], 'fill_colors': Counter(),
        'stroke_colors': Counter(), 'do_calls': Counter(), 'text_samples': [],
        'fonts': set(), 'text_events': 0, 'text_chars': 0, 'text_positions': [],
        'bbox': None, 'fill_events': 0, 'stroke_events': 0,
        'path_starts': 0, 'line_segs': 0, 'curve_segs': 0, 'rect_segs': 0,
        'fill_areas': [], 'stroke_lens': [],
    }


def upd_bbox(fr, p):
    bx = fr['bbox']
    if bx is None:
        fr['bbox'] = [p[0], p[1], p[0], p[1]]
        return
    bx[0] = min(bx[0], p[0])
    bx[1] = min(bx[1], p[1])
    bx[2] = max(bx[2], p[0])
    bx[3] = max(bx[3], p[1])


def sample_text(fr, raw):
    if not raw:
        return
    text = None
    if raw.startswith(b'\xfe\xff'):
        try:
            text = raw[2:].decode('utf-16-be')
        except Exception:
            text = None
    elif raw.startswith(b'\xff\xfe'):
        try:
            text = raw[2:].decode('utf-16-le')
        except Exception:
            text = None
    if text is None:
        text = raw.decode('latin1')
    fr['text_chars'] += len(text)
    if text not in fr['text_samples'] and len(fr['text_samples']) < 40:
        fr['text_samples'].append(text)


class Walker:
    def __init__(self, tokens):
        self.tokens = tokens
        self.ctm_stack = [ID]
        self.frames = []
        self.root = new_frame('ROOT')
        self.stack = []
        self.cur = self.root
        self.nums = []
        self.last_names = []
        self.in_bt = False
        self.fill_color = (0.0, 0.0, 0.0)
        self.stroke_color = (0.0, 0.0, 0.0)
        self.poly = []
        self.poly_len = 0.0

    def take(self, k):
        out = self.nums[-k:] if len(self.nums) >= k else []
        del self.nums[-k:]
        return out

    def run(self):
        recent = []
        for tok in self.tokens:
            recent.append(tok)
            if len(recent) > 12:
                recent.pop(0)
            if NUM.match(tok):
                self.nums.append(float(tok))
                continue
            if tok in (b'<<', b'>>') or tok.startswith(b'<'):
                self.nums = []
                continue
            if tok.startswith(b'/'):
                self.last_names.append(tok[1:])
                if len(self.last_names) > 3:
                    self.last_names.pop(0)
                self.nums = []
                continue
            if tok.startswith(b'('):
                if self.in_bt:
                    sample_text(self.cur, unescape_pdf_string(tok[1:-1]))
                    self.cur['text_events'] += 1
                self.nums = []
                continue
            if tok.startswith(b'['):
                if self.in_bt:
                    merged = b''.join(unescape_pdf_string(t[1:-1]) for t in
                                      re.findall(rb'\((?:\\.|[^\\()\n])*\)', tok))
                    sample_text(self.cur, merged)
                    self.cur['text_events'] += 1
                self.nums = []
                continue
            kw = tok
            if kw in (b'BDC', b'BMC'):
                is_oc = b'/OC' in recent
                mc = None
                for t in recent:
                    if t.startswith(b'/MC') and t[3:].isdigit():
                        mc = int(t[3:])
                        break
                if mc is None:
                    for i in range(len(recent) - 1):
                        if recent[i] == b'/MC' and NUM.match(recent[i + 1]):
                            mc = int(float(recent[i + 1]))
                if is_oc:
                    fr = new_frame('MC%d' % mc)
                    self.frames.append(fr)
                    self.stack.append(('f', len(self.frames) - 1))
                    self.cur = fr
                else:
                    self.stack.append(('i', None))
                continue
            if kw == b'EMC':
                if self.stack:
                    kind, idx = self.stack.pop()
                    if kind == 'f':
                        parent = self.root
                        for k2, i2 in reversed(self.stack):
                            if k2 == 'f':
                                parent = self.frames[i2]
                        self.cur = parent
                continue
            if kw == b'q':
                self.ctm_stack.append(self.ctm_stack[-1])
            elif kw == b'Q':
                if len(self.ctm_stack) > 1:
                    self.ctm_stack.pop()
            elif kw == b'cm':
                m6 = self.take(6)
                if len(m6) == 6:
                    self.ctm_stack[-1] = mul(tuple(m6), self.ctm_stack[-1])
            elif kw == b'Do':
                self.take(6)
                nm = self.last_names[-1] if self.last_names else b'?'
                self.cur['do_calls'][nm.decode('latin1')] += 1
                self.nums = []
            elif kw in PATH_COORD_OPS:
                vals = self.take(PATH_COORD_OPS[kw])
                fr = self.cur
                fr['ops'][kw] += 1
                ctm = self.ctm_stack[-1]
                if len(vals) != PATH_COORD_OPS[kw]:
                    continue
                if kw == b're':
                    fr['rect_segs'] += 1
                    x, y, w, h = vals
                    for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                        p = pt(px, py, ctm)
                        upd_bbox(fr, p)
                        self.poly.append(p)
                elif kw in (b'm', b'y'):
                    fr['path_starts'] += 1
                    self.poly = [pt(*vals, ctm)]
                    self.poly_len = 0.0
                    upd_bbox(fr, self.poly[-1])
                elif kw == b'l':
                    fr['line_segs'] += 1
                    p = pt(*vals, ctm)
                    if self.poly:
                        self.poly_len += ((p[0] - self.poly[-1][0]) ** 2 +
                                         (p[1] - self.poly[-1][1]) ** 2) ** 0.5
                    self.poly.append(p)
                    upd_bbox(fr, p)
                elif kw == b'c':
                    fr['curve_segs'] += 1
                    p = pt(vals[4], vals[5], ctm)
                    if self.poly:
                        self.poly_len += ((p[0] - self.poly[-1][0]) ** 2 +
                                         (p[1] - self.poly[-1][1]) ** 2) ** 0.5
                    self.poly.append(p)
                    upd_bbox(fr, p)
                elif kw in (b'v', b'u'):
                    p = pt(vals[2], vals[3], ctm)
                    self.poly.append(p)
                    upd_bbox(fr, p)
                if len(self.poly) > 20000:
                    self.poly = []
            elif kw in FILL_OPS:
                fr = self.cur
                fr['ops'][kw] += 1
                fr['fill_events'] += 1
                fr['fill_colors'][tuple(round(v, 2) for v in self.fill_color)] += 1
                if len(self.poly) > 1:
                    fr['fill_areas'].append(poly_area(self.poly))
                self.poly = []
            elif kw in STROKE_OPS:
                fr = self.cur
                fr['ops'][kw] += 1
                fr['stroke_events'] += 1
                fr['stroke_colors'][tuple(round(v, 2) for v in self.stroke_color)] += 1
                fr['stroke_lens'].append(self.poly_len)
                self.poly = []
            elif kw == b'w':
                v = self.take(2)
                if v:
                    fr = self.cur
                    fr['widths'].append(v[0])
            elif kw in (b'W', b'W*'):
                v = self.take(1)
                if v:
                    fr = self.cur
                    fr['widths'].append(v[0])
            elif kw in FILL_COLOR_OPS:
                v = self.take(FILL_COLOR_OPS[kw])
                if len(v) == FILL_COLOR_OPS[kw]:
                    self.fill_color = tuple(v) if kw != b'g' else (v[0], v[0], v[0])
            elif kw in STROKE_COLOR_OPS:
                v = self.take(STROKE_COLOR_OPS[kw])
                if len(v) == STROKE_COLOR_OPS[kw]:
                    self.stroke_color = tuple(v) if kw != b'G' else (v[0], v[0], v[0])
            elif kw == b'BT':
                self.in_bt = True
                self.nums = []
            elif kw == b'ET':
                self.in_bt = False
                self.nums = []
            elif kw == b'Tf':
                sz = self.take(1)
                if self.last_names:
                    self.cur['fonts'].add(self.last_names[-1].decode('latin1'))
                self.nums = []
                if sz:
                    self.cur.setdefault('font_sizes', []).append(sz[0])
            elif kw == b'Tm':
                v = self.take(6)
                if len(v) == 6 and self.in_bt:
                    p = pt(v[4], v[5], self.ctm_stack[-1])
                    if len(self.cur['text_positions']) < 800:
                        self.cur['text_positions'].append((round(p[0], 2), round(p[1], 2)))
            elif kw in (b'Td', b'TD', b'T*', b"'", b'"'):
                pass
            elif kw == b'gs':
                self.nums = []
        return self.frames, self.root


def find_dict_end(text, open_idx):
    depth = 0
    i = open_idx
    L = len(text)
    while i < L:
        if text[i] == '<' and i + 1 < L and text[i + 1] == '<':
            depth += 1
            i += 2
            continue
        if text[i] == '>' and i + 1 < L and text[i + 1] == '>':
            depth -= 1
            if depth == 0:
                return i + 2
            i += 2
            continue
        i += 1
    return -1


def ref_list(seg, key):
    pat = key.encode() + rb'\s*\[((?:[^\[\]]|\\.)*?)\]'
    m = re.search(pat, seg)
    if not m:
        return []
    return [int(x) for x in re.findall(rb'(\d+)\s+0\s+R', m.group(1))]


def analyze_file(path):
    data = open(path, 'rb').read()
    offs = parse_xref_offsets(data)
    root_xr = offs.get('__root__')
    objs, _ = slice_objects(data, offs)
    out = {'file': path, 'bytes': len(data),
           'pdf_ver': data[:data.find(b'%PDF-')].decode('latin1'),
           'obj_count': len(objs)}
    rh = objs[root_xr]['head']
    pages_m = re.search(r'/Pages\s+(\d+)\s+0\s+R', rh)
    pagenode = objs[int(pages_m.group(1))]['head']
    kids = re.findall(r'(\d+)\s+0\s+R', re.search(r'/Kids\s*\[([^\]]*)\]', pagenode).group(1))
    page_xr = int(kids[0])
    ph = objs[page_xr]['head']
    mb = re.search(r'/MediaBox\s*\[([^\]]*)\]', ph)
    media_box = [float(v) for v in mb.group(1).split()]
    art = re.search(r'/ArtBox\s*\[([^\]]*)\]', ph)
    out['media_box'] = media_box
    out['art_box'] = [float(v) for v in art.group(1).split()] if art else None
    cm = re.search(r'/Contents\s+(\d+)\s+0\s+R', ph)
    content_stream = objs[int(cm.group(1))]['payload'] or b''
    props = {int(i): int(x) for i, x in
             re.findall(rb'/MC(\d+)\s+(\d+)\s+0\s+R',
                         data[objs[page_xr] and offs[page_xr]:offs[page_xr] + 4096])}
    ocprops_i = rh.find('/OCProperties')
    opseg = data[offs[root_xr]:][ocprops_i:ocprops_i + 3000]
    ocgs_all = ref_list(opseg, '/OCGs')
    d_i = opseg.find(b'/D')
    d_seg = opseg[d_i:d_i + 3000]
    on_default = ref_list(d_seg, '/ON')
    off_default = ref_list(d_seg, '/OFF')
    layers = []
    for idx, xr in enumerate(ocgs_all):
        o = data[offs[xr]:offs[xr] + 2048]
        eob = o.find(b'endobj')
        raw = o[:eob] if eob > 0 else o
        nm_m = re.search(rb'/Name\s*\(((?:\\.|[^\\()\n])*)\)', raw)
        name, enc = ('?', '?')
        if nm_m:
            name, enc = decode_name(unescape_pdf_string(nm_m.group(1)))
        st = 'on'
        if xr in on_default:
            st = 'on-d'
        if xr in off_default:
            st = 'off'
        layers.append({'idx': idx, 'xref': xr, 'name': name, 'enc': enc,
                       'state': st})
    out['layers'] = layers
    walker = Walker(tokenize(content_stream))
    frames, root_frame = walker.run()
    fby_mc = {}
    for fr in frames:
        m = re.match(r'MC(\d+)$', fr['label'])
        fby_mc[int(m.group(1))] = fr
    prop_mc_by_xr = {xr: mc for mc, xr in props.items()}
    for L in layers:
        mc = prop_mc_by_xr.get(L['xref'])
        L['mc'] = mc
        L['frame'] = fby_mc.get(mc) if mc is not None else None
    out['frames_n'] = len(frames)
    out['root_frame'] = root_frame
    fm_names = {}
    for k, ob in objs.items():
        if isinstance(k, str):
            continue
        hm = re.findall(rb'/(Fm\w+)\s+(\d+)\s+0\s+R',
                        data[offs[k]:offs[k] + 2048])
        for nm, xr in hm:
            fm_names.setdefault(nm.decode('latin1'), int(xr))
    forms = []
    for k, ob in objs.items():
        if isinstance(k, str):
            continue
        head_b = data[offs[k]:offs[k] + 512]
        if re.search(rb'/Subtype\s*/Form', head_b):
            names = [n for n, x in fm_names.items() if x == k]
            fl = len(ob['payload'] or b'')
            walk2 = Walker(tokenize(ob['payload'] or b''))
            _, rf = walk2.run()
            forms.append({
                'xref': k, 'names': sorted(names), 'stream_bytes': fl,
                'paths': rf['path_starts'], 'fills': rf['fill_events'],
                'strokes': rf['stroke_events'], 'rects': rf['rect_segs'],
                'lines': rf['line_segs'], 'curves': rf['curve_segs'],
                'max_width': max(rf['widths']) if rf['widths'] else 0,
                'bbox': rf['bbox'],
                'fill_colors': rf['fill_colors'].most_common(4),
                'stroke_colors': rf['stroke_colors'].most_common(4),
            })
    forms.sort(key=lambda f: -f['stream_bytes'])
    out['forms'] = forms
    return out, content_stream


def frame_line(fr):
    if fr is None:
        return dict(paths=0, fills=0, strokes=0, rects=0, lines=0, curves=0,
                    widths=[], fill_colors=[], stroke_colors=[], do_calls={},
                    text_events=0, text_chars=0, bbox='-',
                    top_fill=[], top_stroke=[], texts=[], fonts=[],
                    text_positions=[])
    w = fr['widths']
    wtop = sorted(set(w), reverse=True)[:6]
    fa = sorted(fr['fill_areas'], reverse=True)[:5]
    sl = sorted(fr['stroke_lens'], reverse=True)[:5]
    return dict(
        paths=fr['path_starts'], fills=fr['fill_events'], strokes=fr['stroke_events'],
        rects=fr['rect_segs'], lines=fr['line_segs'], curves=fr['curve_segs'],
        widths=[round(v, 2) for v in wtop],
        width_hist_buckets=Counter(round(v) for v in w),
        fill_colors=fr['fill_colors'].most_common(8),
        stroke_colors=fr['stroke_colors'].most_common(6),
        do_calls=fr['do_calls'],
        text_events=fr['text_events'], text_chars=fr['text_chars'],
        bbox=['%.0f' % v for v in fr['bbox']] if fr['bbox'] else '-',
        top_fill=['%.0f' % v for v in fa],
        top_stroke=['%.0f' % v for v in sl],
        texts=fr['text_samples'][:24],
        fonts=sorted(fr['fonts']),
        text_positions=fr['text_positions'],
    )


def report(out):
    R = []
    R.append('# Layer inventory: %s' % out['file'].split('\\')[-1])
    R.append('')
    R.append('- bytes=%d pdf=%s objs=%d media=%s' %
             (out['bytes'], out['pdf_ver'], out['obj_count'], out['media_box']))
    if out['art_box']:
        R.append('- artbox=%s' % out['art_box'])
    R.append('- OCG layers=%d content-blocks-found=%d' %
             (len(out['layers']), out['frames_n']))
    R.append('')
    hdr = ('| MC | obj | state | name | paths | rects | lines | curves | fills | '
           'strokes | widths(top) | Do calls | txt ev/ch | bbox |')
    R.append(hdr)
    R.append('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')
    for L in out['layers']:
        d = frame_line(L['frame'])
        dc = ','.join('%s×%d' % (k, v) for k, v in
                      sorted(L['frame']['do_calls'].items(),
                             key=lambda kv: -kv[1])[:6]) if L['frame'] and L['frame']['do_calls'] else '-'
        fc = L['frame']
        bw = ','.join(str(w) for w in d['widths']) if d['widths'] else '-'
        R.append('| %s | %d | %s | %s | %d | %d | %d | %d | %d | %d | %s | %s | %d/%d | %s |' %
                 (L['mc'], L['xref'], L['state'], L['name'],
                  d['paths'], d.get('rects', 0), d.get('lines', 0), d.get('curves', 0),
                  d['fills'], d['strokes'], bw, dc,
                  d['text_events'], d['text_chars'], d['bbox']))
    R.append('')
    R.append('## Per-layer detail')
    for L in out['layers']:
        d = frame_line(L['frame'])
        if not d['fills'] and not d['strokes'] and not d['texts'] and \
                not (L['frame'] and L['frame']['do_calls']):
            R.append('- **%s** (obj %d, %s): empty/absent block.' %
                     (L['name'], L['xref'], L['state']))
            continue
        R.append('- **%s** (obj %d, %s)' % (L['name'], L['xref'], L['state']))
        if d['fill_colors']:
            cc = ', '.join('%s ×%d' % (str(c), n) for c, n in d['fill_colors'][:6])
            R.append('  - fill colors: %s' % cc)
        if d['stroke_colors']:
            cc = ', '.join('%s ×%d' % (str(c), n) for c, n in d['stroke_colors'][:6])
            R.append('  - stroke colors: %s' % cc)
        if d['do_calls']:
            dc = ', '.join('%s ×%d' % (k, v) for k, v in d['do_calls'].most_common(8))
            R.append('  - Do calls: %s' % dc)
        if d['top_fill']:
            R.append('  - top fill areas: %s' % ', '.join(d['top_fill']))
        if d['top_stroke']:
            R.append('  - top stroke lens: %s' % ', '.join(d['top_stroke']))
        if d['fonts']:
            R.append('  - fonts: %s' % ', '.join(d['fonts']))
        if d['texts']:
            ts = ' | '.join(repr(t)[:40] for t in d['texts'][:14])
            R.append('  - text samples: %s' % ts)
    R.append('')
    R.append('## ROOT-level (outside any /OC block)')
    d = frame_line(out['root_frame'])
    R.append('- paths=%s fills=%s strokes=%s rects=%s lines=%s curves=%s bbox=%s' %
             (d['paths'], d['fills'], d['strokes'], d.get('rects', 0),
              d.get('lines', 0), d.get('curves', 0), d['bbox']))
    if out['root_frame']['do_calls']:
        dc = ', '.join('%s ×%d' % (k, v) for k, v in
                       out['root_frame']['do_calls'].most_common(10))
        R.append('- Do calls: %s' % dc)
    if out['root_frame']['fill_colors']:
        cc = ', '.join('%s ×%d' % (str(c), n) for c, n in
                       out['root_frame']['fill_colors'].most_common(6))
        R.append('- fill colors: %s' % cc)
    R.append('')
    R.append('## Form XObject catalog (total %d)' % len(out['forms']))
    tot = sum(f['stream_bytes'] for f in out['forms'])
    R.append('- total payload bytes = %d; with fills = %d; with strokes = %d' %
             (tot, sum(1 for f in out['forms'] if f['fills']),
              sum(1 for f in out['forms'] if f['strokes'])))
    R.append('| name(s) | xref | bytes | paths | fills | strokes | rects | max-w | bbox |')
    R.append('|---|---|---|---|---|---|---|---|---|')
    for f in out['forms'][:25]:
        R.append('| %s | %d | %d | %d | %d | %d | %d | %.2f | %s |' %
                 ('+'.join(f['names'][:3]), f['xref'], f['stream_bytes'],
                  f['paths'], f['fills'], f['strokes'], f['rects'],
                  f['max_width'],
                  'x'.join(str(int(v)) for v in f['bbox']) if f['bbox'] else '-'))
    R.append('')
    return '\n'.join(R)


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    args = sys.argv[1:]
    jp = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == '--json':
            jp = args[i + 1]
            i += 2
            continue
        rest.append(args[i])
        i += 1
    results = []
    md_parts = []
    for path in rest:
        out, _ = analyze_file(path)
        md_parts.append(report(out))
        j = {
            'file': out['file'], 'media_box': out['media_box'],
            'art_box': out['art_box'],
            'layers': [{k: v for k, v in L.items() if k != 'frame'} |
                       {'frame': frame_line(L['frame'])} for L in out['layers']],
            'root_frame': frame_line(out['root_frame']),
            'forms': out['forms'],
        }
        results.append(j)
    print('\n\n'.join(md_parts))
    if jp:
        json.dump(results, open(jp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('[json written %s]' % jp)


if __name__ == '__main__':
    main()
