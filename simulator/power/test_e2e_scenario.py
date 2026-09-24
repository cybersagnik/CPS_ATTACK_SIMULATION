#!/usr/bin/env python3
"""End-to-end scenario tests for simulator/power/normal_scenario.py.

Requires ``opendssdirect`` and the generated Ausgrid artifacts; skipped unless
``RUN_E2E=1`` is exported. Run from the repo root:

    RUN_E2E=1 python3 -m unittest simulator.power.test_e2e_scenario
"""

import csv
import datetime as dt
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HAS_DSS = False
try:
    import opendssdirect  # noqa: F401
    HAS_DSS = True
except Exception:  # pragma: no cover
    HAS_DSS = False

ASSIGNMENT = {
    "ieee37": os.path.join(REPO, "grid_data", "profile_assignment_25_seed42.csv"),
    "ieee123": os.path.join(REPO, "grid_data", "profile_assignment_85_seed42.csv"),
}
PROFILES = os.path.join(REPO, "grid_data", "ausgrid_profiles_2010_2011.csv")

SKIP = not (HAS_DSS and os.environ.get("RUN_E2E") == "1"
            and os.path.exists(PROFILES))


@unittest.skipIf(SKIP, "set RUN_E2E=1 with opendssdirect and Ausgrid artifacts")
class RunScenarioE2ETest(unittest.TestCase):

    def _assert_feeder_window(self, feeder_id, days, start):
        from simulator.power.normal_scenario import run
        with tempfile.TemporaryDirectory() as out:
            manifest = run(feeder_id, ASSIGNMENT[feeder_id], PROFILES,
                           dt.datetime(*start), days, out)
            self.assertEqual(manifest["window"]["steps"], days * 48)
            short = f"normal_{feeder_id}"
            with open(os.path.join(out, f"{short}_summary.csv"), newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(all(r["converged"] == "1" for r in rows))
            self.assertEqual(len(rows), days * 48)
            with open(os.path.join(out, f"{short}_bus_telemetry.csv"), newline="") as f:
                telemetry = list(csv.DictReader(f))
            self.assertGreater(len(telemetry), 0)
            for r in telemetry:
                for k in ("vpu_AB", "vpu_BC", "vpu_CA"):
                    if r[k]:
                        v = float(r[k])
                        self.assertGreater(v, 0.0)
                        self.assertLess(v, 1.5)
            with open(os.path.join(out, f"{short}_current_telemetry.csv"),
                      newline="") as f:
                currents = list(csv.DictReader(f))
            self.assertGreater(len(currents), 0)
            self.assertEqual(
                set(currents[0]) - {"timestamp", "feeder_id", "element",
                                    "element_type", "bus", "phase", "i_amps"},
                set())
            self.assertTrue(all(r["phase"] in ("A", "B", "C") for r in currents))
            self.assertTrue(all(float(r["i_amps"]) >= 0.0 for r in currents))
            self.assertTrue(any(r["element_type"] == "source" for r in currents))

    def test_ieee37_one_day_converges_and_is_nan_free(self):
        self._assert_feeder_window("ieee37", 1, (2010, 7, 15))

    def test_ieee123_one_day_converges_and_is_nan_free(self):
        self._assert_feeder_window("ieee123", 1, (2010, 7, 15))


if __name__ == "__main__":
    unittest.main()