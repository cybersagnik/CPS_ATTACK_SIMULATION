#!/usr/bin/env python3
"""Unit tests for simulator/power/dss_builder.py (Phase D translation).

Pure -- no OpenDSS required. Run:

    python3 -m unittest simulator.power.test_dss_builder -v
"""

import unittest

from simulator.power.loader import FeederLoader
from simulator.power.dss_builder import DssBuilder


class DssBuilderTranslationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.models = {fid: FeederLoader().load(fid) for fid in ("ieee37", "ieee123")}

    def test_line_length_converts_thousandth_of_mile_to_miles(self):
        m = self.models["ieee37"]
        l1 = next(l for l in m.lines if l.source_id == "701-702")
        self.assertAlmostEqual(l1.length_ft / 1000.0, 0.96, places=6)
        cmd = "\n".join(DssBuilder(m).build_commands())
        self.assertIn("Bus1=701 Bus2=702", cmd)
        self.assertIn("Length=0.96", cmd)

    def test_xfm1_terminal_order_is_high_then_low(self):
        for fid, expected in (("ieee37", ("709", "775")),
                              ("ieee123", ("61", "610"))):
            m = self.models[fid]
            t = next(t for t in m.transformers if "XFM" in t.source_id.upper())
            endpoints = {bus for line in m.lines for bus in (line.bus1, line.bus2)}
            builder = DssBuilder(m)
            buses = builder._resolved_buses(t)  # applies override table for ''
            hi, lo = builder._terminal_order(t, buses, endpoints,
                                             float(m.nominal_kv))
            self.assertEqual((hi, lo), expected, fid)

    def test_floating_stub_identification(self):
        from simulator.power.normal_scenario import _floating_stubs
        self.assertIn("610", _floating_stubs(self.models["ieee123"]))
        self.assertIn("775", _floating_stubs(self.models["ieee37"]))

    def test_bus_nominal_kv_map(self):
        from simulator.power.normal_scenario import _bus_nominal_kv
        self.assertAlmostEqual(_bus_nominal_kv(self.models["ieee123"])["610"], 0.48)
        self.assertAlmostEqual(_bus_nominal_kv(self.models["ieee37"])["701"], 4.8)

    def test_load_element_map_counts(self):
        for fid, expected in (("ieee37", 32), ("ieee123", 91)):
            b = DssBuilder(self.models[fid])
            b.build_commands()
            total = sum(len(v) for v in b.load_element_map().values())
            self.assertEqual(total, expected, fid)

    def test_load_target_order_is_user_sorted_get_load_targets(self):
        from simulator.power.normal_scenario import _order_load_uids
        for fid in ("ieee37", "ieee123"):
            m = self.models[fid]
            order = _order_load_uids(m)
            self.assertEqual(order, list(m.get_load_targets()[fid].keys()), fid)


if __name__ == "__main__":
    unittest.main()