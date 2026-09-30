r"""P5-T review application: removals + render-snaps onto calibrated walls.

Deterministic offline step between P3 calibration and the corridors.json
merge. Inputs are the P3 pixel walls plus the PDF-space review file
(docs/wall_review_override.json); PDF-space geometry hashes are stable
across re-runs, so review decisions survive re-extraction.

Review file schema (per floor):
  {"removed": [{"hash", "reason", "px"}...],
   "shifts": {"<hash>": {"dx", "dy", "reason", "px", ...}},
   "trims": {"<hash>": {"keep": [[x1,y1],[x2,y2]], "reason", ...}},
   "splits": {"<hash>": {"pieces": [[[x1,y1],[x2,y2]], ...], "reason", ...}}}

Trims shorten a wall to a keep-interval (pixel space, applied after
shifts): interval endpoints must lie on the wall line within 1 px and
inside the wall bounds, and the kept length must be >= 10 px, else
fail fast. Splits generalize trims to several kept pieces (door gaps):
same validation per piece; pieces must not overlap.

Usage:
  python apply_wall_review.py --floor 6F \
      --walls-pdf ..\\extract_out\\6F\\walls_pdf.json \
      --walls-px ..\\extract_out\\6F\\walls_px_final.json \
      --review ..\\docs\\wall_review_override.json \
      --out-walls ..\\extract_out\\6F\\walls_px_snapped.json [--merge]
  With --merge, also patches --corridors in place (CRLF preserved).
"""
import argparse
import hashlib
import json
import os
import sys


def seg_hash(a, b):
    pa = (round(float(a[0]), 1), round(float(a[1]), 1))
    pb = (round(float(b[0]), 1), round(float(b[1]), 1))
    if pa > pb:
        pa, pb = pb, pa
    return hashlib.sha1(('%r|%r' % (pa, pb)).encode('ascii')).hexdigest()[:12]


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--floor', required=True)
    ap.add_argument('--walls-pdf', required=True)
    ap.add_argument('--walls-px', required=True)
    ap.add_argument('--review', required=True)
    ap.add_argument('--out-walls', required=True)
    ap.add_argument('--corridors', default=None)
    ap.add_argument('--merge', action='store_true')
    ap.add_argument('--report-json', default=None,
                    help='P2 report.json: removal hashes already consumed by '
                         'the extractor (recorded under stats) are tolerated '
                         'here; anything else unmatched still fails fast.')
    args = ap.parse_args()

    pdf = load_json(args.walls_pdf)['segments']
    pxdoc = load_json(args.walls_px)
    px = pxdoc['segments']
    assert len(pdf) == len(px), 'pdf/px segment count mismatch'
    review = load_json(args.review)
    fdata = (review.get('floors') or {}).get(args.floor) or {}
    removed = {e['hash'] for e in (fdata.get('removed') or []) if e.get('hash')}
    shifts = dict(fdata.get('shifts') or {})
    trims = dict(fdata.get('trims') or {})
    splits = dict(fdata.get('splits') or {})

    import math

    def check_interval(ax, ay, bx, by, qx, qy, h):
        L = math.hypot(bx - ax, by - ay)
        if L <= 0:
            raise SystemExit('review op on zero-length wall %s' % h)
        u = ((qx - ax) * (bx - ax) + (qy - ay) * (by - ay)) / (L * L)
        perp = math.hypot(qx - (ax + u * (bx - ax)),
                          qy - (ay + u * (by - ay)))
        if perp > 1.0 or not (-1.0 <= u * L <= L + 1.0):
            raise SystemExit('review keep-point off wall %s: %s' % (h, (qx, qy)))
        return u * L
    hashes = [seg_hash(a, b) for a, b in pdf]
    if len(set(hashes)) != len(hashes):
        raise SystemExit('duplicate PDF segment hashes; refusing to apply')
    seen = {}
    out = []
    n_removed, n_shifted, n_trimmed = 0, 0, 0
    for h, seg in zip(hashes, px):
        if h in removed:
            n_removed += 1
            seen[h] = 'removed'
            continue
        if h in shifts:
            dx = float(shifts[h]['dx'])
            dy = float(shifts[h]['dy'])
            seg = [[[round(seg[0][0] + dx, 1), round(seg[0][1] + dy, 1)],
                    [round(seg[1][0] + dx, 1), round(seg[1][1] + dy, 1)]]][0]
            n_shifted += 1
            seen[h] = 'shifted'
        if h in trims:
            keep = trims[h]['keep']
            (ax, ay), (bx, by) = seg
            L = math.hypot(bx - ax, by - ay)
            (kax, kay), (kbx, kby) = keep
            for qx, qy in ((kax, kay), (kbx, kby)):
                check_interval(ax, ay, bx, by, qx, qy, h)
            kl = math.hypot(kbx - kax, kby - kay)
            if kl < 10.0:
                raise SystemExit('trim keep-length too short %s' % h)
            seg = [[round(kax, 1), round(kay, 1)],
                   [round(kbx, 1), round(kby, 1)]]
            n_trimmed += 1
            seen[h] = seen.get(h, 'kept') + '+trimmed'
        if h in splits:
            (ax, ay), (bx, by) = seg
            spans = []
            for (kax, kay), (kbx, kby) in splits[h]['pieces']:
                ta = check_interval(ax, ay, bx, by, kax, kay, h)
                tb = check_interval(ax, ay, bx, by, kbx, kay, h)
                if abs(tb - ta) < 10.0:
                    raise SystemExit('split piece too short %s' % h)
                spans.append(sorted((ta, tb)))
            spans.sort()
            for s1, s2 in zip(spans, spans[1:]):
                if s2[0] < s1[1] - 1.0:
                    raise SystemExit('split pieces overlap %s' % h)
            L = math.hypot(bx - ax, by - ay)
            ux, uy = (bx - ax) / L, (by - ay) / L
            for t0, t1 in spans:
                out.append([[round(ax + ux * t0, 1), round(ay + uy * t0, 1)],
                            [round(ax + ux * t1, 1), round(ay + uy * t1, 1)]])
            n_trimmed += 1
            seen[h] = seen.get(h, 'kept') + '+split'
            continue
        out.append(seg)
    missing_removed = sorted(removed - set(seen))
    missing_shifts = sorted(set(shifts) - set(seen))
    if missing_removed:
        consumed = set()
        if args.report_json:
            rep = load_json(args.report_json)
            consumed = set(((rep.get('stats') or {}).get(
                'review_removed_hashes')) or [])
        unexplained = [h for h in missing_removed if h not in consumed]
        if unexplained:
            raise SystemExit('removal hashes matched nothing: %s' % unexplained)
        print('note: %d removal(s) already consumed upstream (extractor)' %
              len(missing_removed))
    if missing_shifts:
        raise SystemExit('shift hashes matched nothing: %s' % missing_shifts)
    missing_trims = sorted(set(trims) - set(seen))
    if missing_trims:
        raise SystemExit('trim hashes matched nothing: %s' % missing_trims)
    missing_splits = sorted(set(splits) - set(seen))
    if missing_splits:
        raise SystemExit('split hashes matched nothing: %s' % missing_splits)
    print('floor %s: %d in -> %d out (%d removed, %d shifted, %d trimmed)' %
          (args.floor, len(px), len(out), n_removed, n_shifted, n_trimmed))

    with open(args.out_walls, 'w', encoding='utf-8', newline='') as f:
        json.dump({'space': pxdoc.get('space', '') + ' + P5-T review snaps',
                   'segments': out}, f, indent=1)
        f.write('\n')
    print('wrote', args.out_walls)

    if args.merge:
        if not args.corridors:
            raise SystemExit('--merge needs --corridors')
        with open(args.corridors, 'r', encoding='utf-8', newline='') as f:
            raw = f.read()
        newl = '\r\n' if '\r\n' in raw else '\n'
        corridors = json.loads(raw)
        corridors[args.floor]['walls'] = out
        # Byte recipe (verified round-trip identical on untouched input):
        # indent=2, CRLF newlines, no trailing newline.
        text = json.dumps(corridors, ensure_ascii=False, indent=2)
        with open(args.corridors, 'w', encoding='utf-8', newline='') as f:
            f.write(text.replace('\n', newl))
        print('merged %d walls into %s [%s]' % (len(out), args.corridors, args.floor))


if __name__ == '__main__':
    main()
