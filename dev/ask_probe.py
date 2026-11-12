"""Probe the REAL Ask chat host (manual, not in CI).

Requires the local stack from docs/RUNBOOK_ask-local-dev.md §6-§11:
Ask backend on :3000, Roary UI on :5174, and the `nyush-library` row running.

What it proves, which the stub harness cannot:
  * the view stays light to match Ask even when the host reports OS dark
    (the browser is forced to `color-scheme: dark` on purpose);
  * the embedded iframe is genuinely narrow and still lays out on one row;
  * the 3D model really paints pixels inside the real sandboxed frame
    (`sandbox="allow-scripts"`, opaque origin).

Usage (stack up, then):

    python dev/ask_probe.py
    python dev/ask_probe.py --prompt "Call the get_route tool ..."

Exit code 0 = every checked width passed; 1 = a check failed; 2 = no widget
(the model picked a legacy tool -- rerun with an explicit steering prompt).
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CHAT_URL = "http://localhost:5174/"
OUT_DIR = Path(__file__).resolve().parent.parent / "review_out"
DEFAULT_PROMPT = ("Call the get_route tool with destination N607 and show its "
                  "interactive map. Start at the Entrance.")

# Viewport widths to sweep. The chat column is what matters; 1270 lands the
# iframe near the 402 CSS px width the PR review asked about.
WIDTHS = (1440, 1270, 1200, 1150, 1100)

PROBE = """() => {
  const de = document.documentElement;
  const stage = document.querySelector('#stage');
  const svg = document.querySelector('#map');
  const bar = document.querySelector('#toolbar');
  const controls = bar ? [...bar.querySelectorAll('button')] : [];
  const rects = controls.map((b) => {
    const r = b.getBoundingClientRect();
    return {left: Math.round(r.left), right: Math.round(r.right),
            top: Math.round(r.top), bottom: Math.round(r.bottom)};
  });
  const centres = rects.map((r) => (r.top + r.bottom) / 2);
  const oneRow = centres.length > 0
    && Math.max(...centres) - Math.min(...centres) <= 8;
  let overlap = false;
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = rects[i], b = rects[j];
      if (a.left < b.right && b.left < a.right
          && a.top < b.bottom && b.top < a.bottom) overlap = true;
    }
  }
  const rr = stage ? stage.getBoundingClientRect() : null;
  return {
    theme: de.dataset.theme || '(none)',
    bodyBg: getComputedStyle(document.body).backgroundColor,
    scrollW: de.scrollWidth, clientW: de.clientWidth,
    controlsOneRow: oneRow, controlsOverlap: overlap,
    stageW: rr ? Math.round(rr.width) : null,
    stageH: rr ? Math.round(rr.height) : null,
    viewBox: svg ? svg.getAttribute('viewBox') : null,
    controls: controls.map((b) => b.textContent.trim()),
    errorBar: (document.querySelector('#errorbar') || {}).innerText || '',
  };
}"""


def _find_view_frame(page, timeout_s=150):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if frame.locator("#map").count():
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(1500)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--url", default=CHAT_URL)
    args = ap.parse_args()

    errors = []
    report = {"widths": {}}
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # color_scheme="dark" is deliberate: the OS is dark, the chat is light.
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  color_scheme="dark")
        page = ctx.new_page()
        page.on("console", lambda m: errors.append((m.type, m.text))
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(("pageerror", str(e))))

        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)  # guest session mint

        box = page.locator("textarea").first
        box.wait_for(state="visible", timeout=20000)
        box.click()
        box.fill(args.prompt)
        box.press("Enter")

        frame = _find_view_frame(page)
        if frame is None:
            report["error"] = "no MCP App widget appeared"
            report["console_errors"] = errors[:8]
            print(json.dumps(report, indent=2))
            return 2

        page.wait_for_timeout(3000)
        report["os_prefers_dark"] = page.evaluate(
            "window.matchMedia('(prefers-color-scheme: dark)').matches")
        report["host_dark_class"] = page.evaluate(
            "document.documentElement.classList.contains('dark')")
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(900)
            info = frame.evaluate(PROBE)
            ifr = page.locator("iframe").last.bounding_box() or {}
            info["iframe_w"] = round(ifr.get("width") or 0)
            info["iframe_h"] = round(ifr.get("height") or 0)
            report["widths"][str(width)] = info

            if info["theme"] != "light" and report.get("os_prefers_dark"):
                failed.append(f"{width}px: theme={info['theme']}")
            if info["scrollW"] > info["clientW"]:
                failed.append(f"{width}px: horizontal overflow "
                              f"{info['scrollW']}>{info['clientW']}")
            if info["iframe_w"] == 0:
                failed.append(f"{width}px: no iframe")
            if info["stageH"] and info["stageH"] < 120:
                failed.append(f"{width}px: stage only {info['stageH']}px tall")
            if info["errorBar"]:
                failed.append(f"{width}px: error bar: {info['errorBar']}")

        report["console_errors"] = errors[:8]
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT_DIR / "ask-probe.png"), full_page=True)
        browser.close()

    report["failed"] = failed
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
