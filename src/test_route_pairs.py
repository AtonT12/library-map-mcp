"""Table-driven routing regressions against the shipped location/map data.

Run with: PYTHONPATH=src python src/test_route_pairs.py
Add a row to ROUTE_PAIRS to cover another trip. Distances are map pixels,
not metres; the rounded ceilings allow small geometry changes while catching
large detours. Expected transits are fixed, not calculated by the selector
under test.
"""

import math
import unittest

import library


# Start, destination, expected unrestricted transit, normal/access distance caps.
ROUTE_PAIRS = (
    ("W519", "N606", "Central Stairs", 9700, 13700),  # XR -> Scholars
    ("W516", "N607", "Central Stairs", 8800, 12900),  # Visualization -> Researcher
    ("N501", "N601", "West Stairs", 4300, 5300),
    ("N505", "N601", "Elevator", 3800, 3800),  # Elevator can win normally.
    ("N509", "N606", "Central Stairs", 3400, 8200),
    ("W522", "N605", "Central Stairs", 10700, 12900),
    ("ENTRANCE", "N607", "Central Stairs", 3500, 7500),
    ("W519", "W516", None, 650, 650),  # Same-floor 5F.
    ("N601", "N607", None, 4500, 4500),  # Same-floor 6F.
    ("N607", "N607", None, 0, 0),  # Already there.
)


class RoutePairTests(unittest.TestCase):
    def test_location_pairs(self):
        for start, destination, transit, normal_cap, access_cap in ROUTE_PAIRS:
            directions = [(start, destination)]
            if start != destination:
                directions.append((destination, start))
            for origin, target in directions:
                for accessible in (False, True):
                    with self.subTest(start=origin, destination=target,
                                      accessible_only=accessible):
                        self.check_route(origin, target, transit, accessible,
                                         access_cap if accessible else normal_cap)

    def check_route(self, start, destination, transit, accessible, distance_cap):
        plan = library.get_route(destination, start=start,
                                 accessible_only=accessible)
        source = library.find_location(start)
        target = library.find_location(destination)
        source_point = [float(source["x"]), float(source["y"])]
        target_point = [float(target["x"]), float(target["y"])]
        self.assertEqual(plan.origin.point, source_point)
        self.assertEqual(plan.destination.point, target_point)
        self.assertEqual(plan.legs[0].polyline[0], source_point)
        self.assertEqual(plan.legs[-1].polyline[-1], target_point)
        self.assertEqual(plan.warnings, [])

        cross_floor = source["floor"] != target["floor"]
        expected_transit = "Elevator" if accessible and cross_floor else transit
        self.assertEqual(plan.totals.transits_used,
                         [expected_transit] if expected_transit else [])
        self.assertEqual([leg.floor for leg in plan.legs],
                         [source["floor"], target["floor"]] if cross_floor
                         else [source["floor"]])
        self.assertEqual(plan.totals.floor_changes, int(cross_floor))
        self.assertEqual(plan.totals.step_free,
                         expected_transit in (None, "Elevator"))
        if cross_floor:
            # The two legs must meet the matching landing of one structure.
            for leg, point in ((plan.legs[0], plan.legs[0].polyline[-1]),
                               (plan.legs[1], plan.legs[1].polyline[0])):
                landing = next(r for r in library._transit_options(leg.floor)
                               if r["name"] == expected_transit)
                self.assertEqual(point, [float(landing["x"]), float(landing["y"])])
            self.assertEqual(plan.legs[0].transit.direction,
                             "up" if source["floor"] == "5F" else "down")

        measured_length = 0
        for leg in plan.legs:
            for a, b in zip(leg.polyline, leg.polyline[1:]):
                self.assertFalse(library._blocked(leg.floor, a, b),
                                 f"{leg.floor}: {a} -> {b} crosses a wall")
                measured_length += math.dist(a, b)
        self.assertAlmostEqual(plan.totals.distance_px, measured_length)
        self.assertLessEqual(measured_length, distance_cap)
        if start != destination:
            self.assertGreater(measured_length, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
