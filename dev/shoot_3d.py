"""Render the interactive view's 3D mode headlessly, for visual review.

Builds the real view HTML (same bytes the MCP App serves), then drives it
in headless Chrome with a real RoutePlan, and screenshots the result.

Usage:
  PYTHONPATH=src python dev/shoot_3d.py [destination] [width] [height]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))

import library  # noqa: E402
from view import template  # noqa: E402

CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
OUT = os.path.join(ROOT, 'review_out')


def build_page(dest, view_mode):
    plan = library.get_route(dest, view_mode=view_mode)
    data = json.dumps(plan.model_dump(), ensure_ascii=False)
    html = template.build_view_html()
    # Feed the plan the way the host does: a tool-result notification.
    return html.replace(
        'document.addEventListener("DOMContentLoaded", () => View.boot());',
        'document.addEventListener("DOMContentLoaded", () => { View.boot();'
        ' setTimeout(() => View.onMessage({ data: { jsonrpc: "2.0",'
        ' method: "ui/notifications/tool-result", params:'
        ' { structuredContent: %s } } }), 120); });' % data)


def _ver_key(path):
    """Numeric-aware rank from the version dir name (155.0.8059.85 -> (155,0,8059,85))."""
    name = os.path.basename(os.path.dirname(path))
    try:
        parts = tuple(int(p) for p in name.split('.')[:4])
        while len(parts) < 4:
            parts += (0,)
        return parts
    except ValueError:
        return (0, 0, 0, 0)


def _browser_candidates():
    # Never trust one browser path: the stock Chrome launcher stub exits
    # silently when its versioned payload is missing (interrupted auto-
    # update), which looks exactly like "no screenshot written". Prefer the
    # configured path, then fall back to any newer Chromium on the box --
    # Edge ships the same Blink engine and honours every flag used here.
    cands = []
    root = os.path.dirname(os.path.dirname(CHROME))
    if os.path.isdir(root):
        cands += [os.path.join(d, 'chrome.exe')
                  for d in os.listdir(root)
                  if os.path.isfile(os.path.join(d, 'chrome.exe'))]
    edge_root = r"C:\Program Files (x86)\Microsoft\Edge\Application"
    if os.path.isdir(edge_root):
        cands += [os.path.join(d, 'msedge.exe')
                  for d in os.listdir(edge_root)
                  if os.path.isfile(os.path.join(d, 'msedge.exe'))]
    out, seen = [], set()
    for c in [CHROME] + sorted(cands, key=_ver_key, reverse=True):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def shoot(dest, width, height, view_mode, tag):
    os.makedirs(OUT, exist_ok=True)
    page = os.path.join(OUT, '_shoot_%s.html' % tag)
    with open(page, 'w', encoding='utf-8') as f:
        f.write(build_page(dest, view_mode))
    png = os.path.join(OUT, 'view_3d_%s.png' % tag)
    if os.path.exists(png):
        os.remove(png)
    for exe in _browser_candidates():
        subprocess.run([
            exe, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--screenshot=' + png, '--window-size=%d,%d' % (width, height),
            '--virtual-time-budget=4000', '--default-background-color=20242aff',
            'file:///' + page.replace('\\', '/'),
        ], capture_output=True, timeout=180)
        if os.path.exists(png) and os.path.getsize(png) > 0:
            print('used browser:', exe)
            break
    os.remove(page)
    print('wrote', png, os.path.exists(png))


if __name__ == '__main__':
    dest = sys.argv[1] if len(sys.argv) > 1 else 'N607'
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1100
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 760
    modes = sys.argv[4].split(',') if len(sys.argv) > 4 else ['3d']
    for vm in modes:
        shoot(dest, w, h, vm, '%s_%s' % (dest.replace(' ', '_'), vm))
