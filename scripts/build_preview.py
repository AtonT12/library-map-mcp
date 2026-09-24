"""Regenerate dev/preview.html from the current view template (plan Step 5-1).

`dev/preview.html` is a *generated* artifact (gitignored) used to eyeball the
real single-file App view in a browser: open `dev/host.html` and pick
"preview.html (template output)". It does NOT auto-update -- re-run this after
every change to `src/view/viewer.js`, `src/view/template.py`, or the small
base maps, otherwise you are looking at a stale view.

It is byte-identical to what the `ui://nav/library-view.html` resource serves,
so if the two disagree, the server has drifted from the tree (or vice versa).

    python scripts/build_preview.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, 'src'))

from view import template  # noqa: E402  (needs src/ on sys.path first)

OUT = os.path.join(BASE_DIR, 'dev', 'preview.html')


def main():
    html = template.build_view_html()
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'{os.path.relpath(OUT, BASE_DIR).replace(os.sep, "/")}: '
          f'{len(html):,} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
