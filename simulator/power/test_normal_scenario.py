#!/usr/bin/env python3
"""Unit tests for simulator/power/normal_scenario.py (Phase D binding/telemetry).

Pure tests use synthetic CSVs and need no OpenDSS/profiles. The end-to-end
scenario test (RunScenarioE2ETest) is skipped unless both environments are
available and RUN_E2E=1 is set:

    RUN_E2E=1 python3 -m unittest simulator.power.test_normal_scenario -v
"""

import csv
import os
import tempfile
import unittest

from simulator.power.loader import FeederLoader
from simulator.power.normal_scenario import (
    _floating_stubs, _order_load_uids, _read_assignment, _read_assignment_seed,
)


def _make_assignment(path, customers, seed=42):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["assignment_id", "customer_id",
                                          "generator_capacity_kwp", "seed",
                                          "cl_present"])
        w.writeheader()
        for i, c in enumerate(customers, start=1):
            w.writerow({"assignment_id": i, "customer_id": c,
                        "generator_capacity_kwp": 1.0, "seed": seed,
                        "cl_present": "False"})


class AssignmentCsvTests(unittest.TestCase):
    def test_read_assignment_preserves_row_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.csv")
            _make_assignment(p, [7, 2, 99])
            self.assertEqual(_read_assignment(p), [7, 2, 99])
            self.assertEqual(_read_assignment_seed(p), 42)


class BindingTests(unittest.TestCase):
    def test_target_order_matches_assignment_count(self):
        for fid, count in (("ieee37", 25), ("ieee123", 85)):
            m = FeederLoader().load(fid)
            self.assertEqual(len(_order_load_uids(m)), count, fid)

    def test_floating_stub_iteration_is_symmetric(self):
        for fid in ("ieee37", "ieee123"):
            m = FeederLoader().load(fid)
            stubs = _floating_stubs(m)
            self.assertIsInstance(stubs, set)
            self.assertNotIn("SYSsource", stubs)


if __name__ == "__main__":
    unittest.main()