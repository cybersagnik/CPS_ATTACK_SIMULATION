#!/usr/bin/env python3
"""Unit tests for profile_engine.py (Phase B-6).

Uses small synthetic CSV fixtures built in a temporary directory, so the
tests never scan the real 359 MB dataset.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import profile_engine as pe

HEADER = [
    "customer_id", "postcode", "generator_capacity_kwp", "timestamp",
    "gc_kwh", "cl_kwh", "gg_kwh", "load_kwh", "solar_kwh",
    "load_kw", "solar_kw", "row_quality",
]

DAYS = 2
INTERVALS = 48
SAMPLE_COUNT = DAYS * INTERVALS

START = _dt.datetime(2020, 1, 1)
HALF_HOUR = _dt.timedelta(minutes=30)


def _build_fixture(path: Path, customer_specs) -> None:
    """Write an A-4-format CSV into `path`.

    customer_specs: list of dicts:
        {customer_id, postcode, generator_capacity_kwp, has_cl, has_gg}
    """
    with open(path, "w", newline="") as f:
        f.write(",".join(HEADER) + "\n")
        for spec in customer_specs:
            cid = spec["customer_id"]
            postcode = spec["postcode"]
            cap = spec["generator_capacity_kwp"]
            has_cl = spec.get("has_cl", True)
            has_gg = spec.get("has_gg", True)
            for d in range(DAYS):
                day = START + _dt.timedelta(days=d)
                for i in range(INTERVALS):
                    ts = day + _dt.timedelta(minutes=30 * i)
                    gc = 0.100 + 0.001 * (i % 7)
                    cl = 0.200 if has_cl else 0.0
                    gg = 0.050 + 0.002 * (i % 5) if has_gg else 0.0
                    load_kwh = gc + cl
                    solar_kwh = gg
                    load_kw = load_kwh * 2.0
                    solar_kw = solar_kwh * 2.0
                    fields = [
                        str(cid), str(postcode), repr(cap), ts.isoformat(sep="T"),
                        repr(gc), repr(cl), repr(gg),
                        repr(load_kwh), repr(solar_kwh),
                        repr(load_kw), repr(solar_kw), "",
                    ]
                    f.write(",".join(fields) + "\n")


def _make_specs():
    return [
        {"customer_id": 1, "postcode": 2076, "generator_capacity_kwp": 3.78, "has_cl": True, "has_gg": True},
        {"customer_id": 2, "postcode": 2077, "generator_capacity_kwp": 2.04, "has_cl": False, "has_gg": True},
        {"customer_id": 11, "postcode": 2026, "generator_capacity_kwp": 2.04, "has_cl": False, "has_gg": True},
    ]


class ProfileEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="pe_test_")
        cls.csv_path = Path(cls._tmp) / "fixture.csv"
        cls.specs = _make_specs()
        _build_fixture(cls.csv_path, cls.specs)
        cls.eng = pe.ProfileEngine(cls.csv_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # A. initialization does not materialize all rows -------------------------
    def test_init_does_not_materialize_samples(self):
        calls = {"count": 0}
        orig = pe._parse_row

        def counting_parse(fields):
            calls["count"] += 1
            return orig(fields)

        pe._parse_row = counting_parse
        try:
            profile_engine = pe.ProfileEngine(self.csv_path)
        finally:
            pe._parse_row = orig
        self.assertEqual(calls["count"], 0, "init must not parse any row objects")

    # B. customer ID discovery -----------------------------------------------
    def test_customer_id_discovery(self):
        ids = self.eng.get_customer_ids()
        self.assertEqual(ids, [1, 2, 11])

    # C/E. get_customer_profile(1) and sample count ---------------------------
    def test_customer_1_profile(self):
        prof = self.eng.get_customer_profile(1)
        self.assertIsNotNone(prof)
        self.assertEqual(prof.customer_id, 1)
        self.assertEqual(prof.postcode, 2076)
        self.assertEqual(prof.generator_capacity_kwp, 3.78)
        self.assertEqual(len(prof.samples), SAMPLE_COUNT)
        self.assertEqual(prof.samples[0].timestamp, START)
        self.assertEqual(prof.samples[-1].timestamp.isoformat(sep="T"),
                         "2020-01-02T23:30:00")

    # D. unknown customer ID --------------------------------------------------
    def test_unknown_customer_returns_none(self):
        self.assertIsNone(self.eng.get_customer_profile(99999))

    # F. customer 11 retains cl_kwh = 0.0 -------------------------------------
    def test_customer_11_cl_zero(self):
        prof = self.eng.get_customer_profile(11)
        self.assertIsNotNone(prof)
        self.assertTrue(all(s.cl_kwh == 0.0 for s in prof.samples))
        self.assertTrue(any(s.gc_kwh != 0.0 for s in prof.samples))  # still has GC

    # G. timestamp ordering ---------------------------------------------------
    def test_timestamp_ordering(self):
        prof = self.eng.get_customer_profile(1)
        for a, b in zip(prof.samples, prof.samples[1:]):
            self.assertLess(a.timestamp, b.timestamp)

    # H. 30-minute spacing ----------------------------------------------------
    def test_30min_spacing(self):
        prof = self.eng.get_customer_profile(1)
        for a, b in zip(prof.samples, prof.samples[1:]):
            self.assertEqual(b.timestamp - a.timestamp, HALF_HOUR)

    # I. physical identities --------------------------------------------------
    def test_physical_identities(self):
        prof = self.eng.get_customer_profile(1)
        for s in prof.samples:
            self.assertAlmostEqual(s.load_kwh, s.gc_kwh + s.cl_kwh)
            self.assertAlmostEqual(s.solar_kwh, s.gg_kwh)
            self.assertAlmostEqual(s.load_kw, s.load_kwh * 2.0)
            self.assertAlmostEqual(s.solar_kw, s.solar_kwh * 2.0)

    # J. summary totals -------------------------------------------------------
    def test_summary_totals(self):
        s = self.eng.summary()
        total_load = 0.0
        total_solar = 0.0
        customers = set()
        for spec in self.specs:
            cid = spec["customer_id"]
            customers.add(cid)
            for d in range(DAYS):
                for i in range(INTERVALS):
                    gc = 0.100 + 0.001 * (i % 7)
                    cl = 0.200 if spec["has_cl"] else 0.0
                    gg = 0.050 + 0.002 * (i % 5) if spec["has_gg"] else 0.0
                    total_load += gc + cl
                    total_solar += gg
        self.assertEqual(s["total_profiles"], len(customers) * SAMPLE_COUNT)
        self.assertEqual(s["unique_customers"], 3)
        self.assertEqual(s["customer_id_range"], (1, 11))
        self.assertEqual(s["samples_per_customer"], SAMPLE_COUNT)
        self.assertEqual(s["customers_with_cl"], 1)   # only customer 1
        self.assertEqual(s["customers_with_gg"], 3)   # all three customers
        self.assertAlmostEqual(s["total_load_kwh"], total_load)
        self.assertAlmostEqual(s["total_solar_kwh"], total_solar)

    # validation end-to-end ---------------------------------------------------
    def test_validate_passes(self):
        r = self.eng.validate()
        self.assertTrue(r["all_passed"], msg=str(r["violation_examples"]))
        self.assertEqual(r["customers_checked"], 3)
        self.assertEqual(r["total_profiles"], 3 * SAMPLE_COUNT)

    # iter_customer_profiles --------------------------------------------------
    def test_iter_customer_profiles(self):
        ids_out = [c.customer_id for c in self.eng.iter_customer_profiles()]
        self.assertEqual(ids_out, [1, 2, 11])

    # generator capacity preserved --------------------------------------------
    def test_capacity_preserved(self):
        s = self.eng.summary()
        self.assertEqual(s["generator_capacity_range"], (2.04, 3.78))


if __name__ == "__main__":
    unittest.main()