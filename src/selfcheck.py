"""Startup asset self-check for the library MCP server (plan Step 1-b).

Verifies that every local asset the server depends on exists and parses
*before* the server starts listening, so a bad deploy (e.g. a missing
corridors.json) fails fast with a clear message instead of serving
broken routes. Returns a list of problem strings (empty = ok); never
raises, so callers can aggregate and report.

Standalone on purpose: only stdlib + Pillow, no import of library, so it
also runs in `docker build` before the app layer is exercised.
"""

import csv
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, 'locations.csv')
CORRIDORS_FILE = os.path.join(BASE_DIR, 'corridors.json')
MAP_FILES = ('5f_base.jpg', '6f_base.jpg')

REQUIRED_COLUMNS = {
    'type', 'name', 'call_start', 'call_end', 'floor', 'x', 'y', 'map_file',
}
ROUTABLE_TYPES = {'Room', 'Shelf', 'Facility'}


def _check_locations(problems):
    if not os.path.exists(DATA_FILE):
        problems.append(f'missing {os.path.basename(DATA_FILE)}')
        return None
    try:
        with open(DATA_FILE, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            header = set(reader.fieldnames or [])
    except (ValueError, OSError, csv.Error) as e:
        problems.append(f'{os.path.basename(DATA_FILE)} unreadable: {e}')
        return None
    missing = REQUIRED_COLUMNS - header
    if missing:
        problems.append(
            f'{os.path.basename(DATA_FILE)} header missing: '
            f'{sorted(missing)}'
        )
    bad_coords = 0
    routable = 0
    for i, row in enumerate(rows, start=2):
        try:
            int(row['x'])
            int(row['y'])
        except (KeyError, TypeError, ValueError):
            bad_coords += 1
        if row.get('type') in ROUTABLE_TYPES:
            routable += 1
    if bad_coords:
        problems.append(
            f'{os.path.basename(DATA_FILE)} has {bad_coords} row(s) '
            f'with non-integer x/y'
        )
    if not routable:
        problems.append(
            f'{os.path.basename(DATA_FILE)} has no Room/Shelf/Facility rows'
        )
    return rows


def _check_corridors(problems):
    if not os.path.exists(CORRIDORS_FILE):
        problems.append(f'missing {os.path.basename(CORRIDORS_FILE)}')
        return
    try:
        with open(CORRIDORS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except (ValueError, OSError) as e:
        problems.append(f'{os.path.basename(CORRIDORS_FILE)} invalid: {e}')
        return
    if not isinstance(raw, dict) or not raw:
        problems.append(f'{os.path.basename(CORRIDORS_FILE)} has no floors')
        return
    for floor, info in raw.items():
        if not isinstance(info, dict):
            problems.append(f'corridors[{floor}] is not an object')
            continue
        for key in ('map_file', 'waypoints', 'edges'):
            if key not in info:
                problems.append(f'corridors[{floor}] missing {key!r}')
        waypoints = info.get('waypoints', {})
        if not isinstance(waypoints, dict) or not waypoints:
            problems.append(f'corridors[{floor}] has no waypoints')
            waypoints = {}
        for wid, v in waypoints.items():
            try:
                xy = (v['x'], v['y']) if isinstance(v, dict) else (v[0], v[1])
                float(xy[0])
                float(xy[1])
            except (KeyError, TypeError, ValueError, IndexError):
                problems.append(
                    f'corridors[{floor}] waypoint {wid!r} is not a point'
                )
        for edge in info.get('edges', []):
            try:
                a, b = edge
            except (TypeError, ValueError):
                problems.append(
                    f'corridors[{floor}] has a malformed edge: {edge!r}'
                )
                continue
            if a not in waypoints or b not in waypoints:
                problems.append(
                    f'corridors[{floor}] edge ({a!r}, {b!r}) '
                    f'references unknown waypoint'
                )


def _check_maps(problems):
    try:
        from PIL import Image
    except ImportError:
        problems.append('Pillow not installed, cannot verify map images')
        return
    for name in MAP_FILES:
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            problems.append(f'missing {name}')
            continue
        try:
            with Image.open(path) as img:
                img.size
        except OSError as e:
            problems.append(f'{name} unreadable: {e}')


def _check_transit_pairing(rows, problems):
    if not rows:
        return
    by_floor = {}
    for row in rows:
        if row.get('type') == 'Transit' and row.get('call_start'):
            by_floor.setdefault(row.get('floor'), set()).add(
                row['call_start']
            )
    floors = sorted(by_floor)
    if len(floors) < 2:
        problems.append(
            f'Transit rows cover fewer than 2 floors: {floors}'
        )
        return
    common = set.intersection(*(by_floor[f] for f in floors))
    if not common:
        problems.append(
            'no Transit id is paired across floors '
            f'({ {f: sorted(by_floor[f]) for f in floors} }); '
            'cross-floor routing cannot work'
        )


def verify_assets():
    """Check all server assets; return a list of problem strings (empty = ok)."""
    problems = []
    rows = _check_locations(problems)
    _check_corridors(problems)
    _check_maps(problems)
    _check_transit_pairing(rows, problems)
    return problems


def main():
    problems = verify_assets()
    if problems:
        print('asset check failed:', file=sys.stderr)
        for problem in problems:
            print(f'  - {problem}', file=sys.stderr)
        return 1
    print('assets ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
