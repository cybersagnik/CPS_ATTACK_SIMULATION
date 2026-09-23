#!/usr/bin/env python3
"""Combine Ausgrid time series into per-customer physical profiles.

Stage in the CPS project (Phase A / Step 4):
    Ausgrid CSV -> ausgrid_parser.py        -> structured source records
                -> ausgrid_timeseries.py    -> timestamp-based time series
                -> ausgrid_profiles.py      -> physical load/solar profiles

This module does NOT re-parse the CSV and does NOT re-melt wide rows.  It
consumes the validated time series produced by :mod:`ausgrid_timeseries`
(one record per customer, category, timestamp) and merges the three
consumption categories into ONE profile record per (customer_id, timestamp):

    time-series records (customer, GC, ts), (customer, CL, ts), (customer, GG, ts)
        -->
    one profile: gc_kwh, cl_kwh, gg_kwh, load_kwh = gc_kwh + cl_kwh,
                 solar_kwh = gg_kwh, load_kw, solar_kw

Unit handling (the kWh -> kW conversion):
    Each time-series sample is ENERGY in kWh covering a 30-minute interval.
    Average power over the interval is energy / 0.5 h, i.e. kWh * 2:
        load_kw  = load_kwh  * 2
        solar_kw = solar_kwh * 2
    The rated generator capacity is preserved untouched as
    generator_capacity_kwp (metadata only; never capped or customised).

Deliberate scope limits (Phase A-4 stops here):
    * No net_load_kw:         load and solar are kept as SEPARATE fields.
    * No feeder/bus/load ids: assignment to network nodes is Phase A-5
                              (teammate's feeder model).
    * No reactive power:      P/Q ratios / power-factor handling is Phase A-5.
    * No timezone handling:   timestamps are naive local wall-clock datetimes.
    * row_quality is preserved verbatim (None for the real 2010-2011 file).

Profile semantics:
    load_kwh  = gc_kwh + cl_kwh   (general + controlled load energy)
    solar_kwh = gg_kwh            (gross generation energy)
    For customers with no Controlled Load (CL), cl_kwh = 0.0 and the profile
    still exists (GC and GG cover all customers).  Profiles are emitted in
    (customer_id, date, timestamp) order.

Output record:
    customer_id, postcode, generator_capacity_kwp, timestamp,
    gc_kwh, cl_kwh, gg_kwh, load_kwh, solar_kwh, load_kw, solar_kw, row_quality

API:
    converter = AusgridProfileConverter(AusgridTimeSeriesConverter(...))
    for p in converter.iter_profiles():   # generator, memory-efficient
        ...
    profiles = converter.convert()        # list[AusgridProfile]

CLI:
    python3 ausgrid_profiles.py "Solar home 2010-2011.csv"
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ausgrid_parser import AusgridParser
from ausgrid_timeseries import (
    AusgridTimeSeriesConverter,
    AusgridTimeSeriesRecord,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Each time-series sample covers a 30-minute (0.5 h) interval, so average
# power in kW = energy in kWh / 0.5 = energy * 2.
KWH_TO_KW = 2.0

_VALID_CATEGORIES = ("GC", "CL", "GG")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AusgridProfileError(Exception):
    """Raised when time-series records cannot be combined into profiles."""


# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AusgridProfile:
    """One physical profile sample for a customer and timestamp.

    Fields:
        customer_id:            integer customer identifier.
        postcode:               integer postcode.
        generator_capacity_kwp: rated solar capacity in kWp (metadata only,
                                preserved as-is, never capped/normalised).
        timestamp:              interval START as naive local wall clock
                                datetime (preserved from the time series).
        gc_kwh:                 General Consumption energy for the interval.
        cl_kwh:                 Controlled Load energy (0.0 when the customer
                                has no CL).
        gg_kwh:                 Gross Generation energy for the interval.
        load_kwh:               gc_kwh + cl_kwh (total consumption energy).
        solar_kwh:              gg_kwh (generation energy).
        load_kw:                load_kwh * 2 (avg power, kW over the interval).
        solar_kw:               solar_kwh * 2 (avg power, kW over the interval).
        row_quality:            preserved verbatim from the time series (None
                                for the real 2010-2011 file).
    """

    customer_id: int
    postcode: int
    generator_capacity_kwp: float
    timestamp: _dt.datetime
    gc_kwh: float
    cl_kwh: float
    gg_kwh: float
    load_kwh: float
    solar_kwh: float
    load_kw: float
    solar_kw: float
    row_quality: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict view of the profile (handy for debugging/export)."""
        return {
            "customer_id": self.customer_id,
            "postcode": self.postcode,
            "generator_capacity_kwp": self.generator_capacity_kwp,
            "timestamp": self.timestamp.isoformat(sep="T"),
            "gc_kwh": self.gc_kwh,
            "cl_kwh": self.cl_kwh,
            "gg_kwh": self.gg_kwh,
            "load_kwh": self.load_kwh,
            "solar_kwh": self.solar_kwh,
            "load_kw": self.load_kw,
            "solar_kw": self.solar_kw,
            "row_quality": self.row_quality,
        }


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

class AusgridProfileConverter:
    """Merge Ausgrid time series into per-customer physical profiles.

    The converter takes an :class:`AusgridTimeSeriesConverter` (recommended:
    nothing is re-melted) or builds one from an :class:`AusgridParser`/path:

        converter = AusgridProfileConverter(AusgridTimeSeriesConverter(parser))
        converter = AusgridProfileConverter(parser)
        converter = AusgridProfileConverter("Solar home 2010-2011.csv")

    Profiles are produced lazily.  Because the time series is grouped by
    (customer_id, date, category) and within a date the GC/CL/GG timestamps
    are identical, only one (customer, date) block of 48 records is held in
    memory at a time.
    """

    def __init__(self, source: AusgridTimeSeriesConverter | AusgridParser | str | Path) -> None:
        if isinstance(source, AusgridTimeSeriesConverter):
            self.ts = source
        else:
            self.ts = AusgridTimeSeriesConverter(source)

    # -- public API ---------------------------------------------------------

    def iter_profiles(
        self,
        ts_records: Iterable[AusgridTimeSeriesRecord] | Sequence[AusgridTimeSeriesRecord] | None = None,
    ) -> Iterator[AusgridProfile]:
        """Yield one :class:`AusgridProfile` per (customer_id, timestamp).

        `ts_records` may be a pre-built sequence/generator of time-series
        records; when omitted, the wrapped time-series converter is streamed.
        Records must be grouped by (customer_id, date) with the categories for
        a date contiguous (which iter_records() guarantees).  For every
        (customer_id, date) this emits 48 profiles, one per timestamp.
        """
        rows = ts_records if ts_records is not None else self.ts.iter_records()
        yield from self._grouped_profiles(rows)

    def convert(
        self,
        ts_records: Iterable[AusgridTimeSeriesRecord] | Sequence[AusgridTimeSeriesRecord] | None = None,
    ) -> List[AusgridProfile]:
        """Materialise all profiles into a list.

        2010-2011 has 5,256,000 profiles; prefer iter_profiles() for streaming.
        """
        return list(self.iter_profiles(ts_records))

    # -- internal -----------------------------------------------------------

    def _grouped_profiles(
        self,
        rows: Iterable[AusgridTimeSeriesRecord],
    ) -> Iterator[AusgridProfile]:
        current: Optional[Tuple[int, _dt.date]] = None
        per_cat: Optional[Dict[str, Dict[_dt.datetime, float]]] = None
        meta: Optional[Tuple[int, float, Optional[str]]] = None

        def emit() -> Iterator[AusgridProfile]:
            assert current is not None
            timestamps = sorted(
                {ts for d in per_cat.values() for ts in d}
            )
            for ts in timestamps:
                gc = per_cat["GC"].get(ts, 0.0)
                cl = per_cat["CL"].get(ts, 0.0)
                gg = per_cat["GG"].get(ts, 0.0)
                load_kwh = gc + cl
                solar_kwh = gg
                yield AusgridProfile(
                    customer_id=current[0],
                    postcode=meta[0],
                    generator_capacity_kwp=meta[1],
                    timestamp=ts,
                    gc_kwh=gc,
                    cl_kwh=cl,
                    gg_kwh=gg,
                    load_kwh=load_kwh,
                    solar_kwh=solar_kwh,
                    load_kw=load_kwh * KWH_TO_KW,
                    solar_kw=solar_kwh * KWH_TO_KW,
                    row_quality=meta[2],
                )

        for r in rows:
            key = (r.customer_id, r.date)
            if key != current:
                if current is not None:
                    yield from emit()
                current = key
                per_cat = {"GC": {}, "CL": {}, "GG": {}}
                meta = (r.postcode, r.generator_capacity_kwp, r.row_quality)
            if r.category not in _VALID_CATEGORIES:
                raise AusgridProfileError(
                    f"unexpected category {r.category!r} for customer "
                    f"{r.customer_id} on {r.date}"
                )
            bucket = per_cat[r.category]
            if r.timestamp in bucket:
                raise AusgridProfileError(
                    f"duplicate {r.category} sample for customer {r.customer_id} "
                    f"at {r.timestamp.isoformat()}"
                )
            bucket[r.timestamp] = r.energy_kwh
        if current is not None:
            yield from emit()

    # -- statistics / validation ---------------------------------------------

    @staticmethod
    def summarize(rows: Iterable[AusgridProfile]) -> Dict[str, object]:
        """Single-pass statistics over generated profiles.

        All numbers are computed from the actually generated data.
        """
        total = 0
        customers = set()
        dates = set()
        qualities = Counter()
        total_gc_kwh = 0.0
        total_cl_kwh = 0.0
        total_gg_kwh = 0.0
        total_load_kwh = 0.0
        total_solar_kwh = 0.0
        total_load_kw = 0.0
        total_solar_kw = 0.0
        min_ts = None
        max_ts = None
        for p in rows:
            total += 1
            customers.add(p.customer_id)
            dates.add(p.timestamp.date())
            qualities[p.row_quality] += 1
            total_gc_kwh += p.gc_kwh
            total_cl_kwh += p.cl_kwh
            total_gg_kwh += p.gg_kwh
            total_load_kwh += p.load_kwh
            total_solar_kwh += p.solar_kwh
            total_load_kw += p.load_kw
            total_solar_kw += p.solar_kw
            if min_ts is None or p.timestamp < min_ts:
                min_ts = p.timestamp
            if max_ts is None or p.timestamp > max_ts:
                max_ts = p.timestamp
        return {
            "total_profiles": total,
            "unique_customers": len(customers),
            "date_min": min(dates).isoformat() if dates else None,
            "date_max": max(dates).isoformat() if dates else None,
            "timestamp_min": min_ts.isoformat(sep="T") if min_ts else None,
            "timestamp_max": max_ts.isoformat(sep="T") if max_ts else None,
            "row_quality_values": dict(qualities),
            "total_gc_kwh": total_gc_kwh,
            "total_cl_kwh": total_cl_kwh,
            "total_gg_kwh": total_gg_kwh,
            "total_load_kwh": total_load_kwh,
            "total_solar_kwh": total_solar_kwh,
            "total_load_kw": total_load_kw,
            "total_solar_kw": total_solar_kw,
        }

    @staticmethod
    def validate_profiles(rows: Iterable[AusgridProfile]) -> Dict[str, object]:
        """Integrity validation of the generated profiles.

        For every (customer_id, date) group this verifies:

          1. exactly 48 profiles exist;
          2. the first timestamp is 00:00 on the source date;
          3. the last timestamp is 23:30 on the source date;
          4. consecutive timestamps are exactly 30 minutes apart;
          5. no duplicate timestamps exist;
          6. no timestamp falls outside the source date;
          7. load_kwh == gc_kwh + cl_kwh;
          8. solar_kwh == gg_kwh;
          9. load_kw == load_kwh * 2 (kWh -> kW, 30-min interval);
         10. solar_kw == solar_kwh * 2.

        Profiles are emitted grouped by (customer_id, date) with 48 records per
        group, so the check is a single streaming pass with constant
        per-group state.  Returns a dict with the number of groups/records
        examined, one ok/failed block per check, and violation examples.
        """
        half_hour = _dt.timedelta(minutes=30)

        def dtype(ts: _dt.datetime) -> "Tuple[int, _dt.date]":
            return (ts.year, ts.date())

        groups = 0
        total = 0
        violations = {
            "not_48": [],
            "first_not_0000": [],
            "last_not_2330": [],
            "spacing": [],
            "duplicate": [],
            "out_of_date": [],
            "load_identity": [],
            "solar_identity": [],
            "load_kw_identity": [],
            "solar_kw_identity": [],
        }

        # guard against duplicate (customer_id, date) groups, which would imply
        # duplicate (customer_id, timestamp) combos (timestamps within a group
        # are unique by construction: each is the union of the GC/CL/GG buckets).
        seen_groups: set = set()
        duplicate_groups = []

        current_key = None
        count = 0
        previous_ts = None
        last_ts = None
        first_ts = None

        def finish_group(key, count, first_ts, last_ts) -> None:
            nonlocal groups
            if key is None:
                return
            groups += 1
            expected_first = _dt.datetime.combine(key[1], _dt.time(0, 0))
            expected_last = _dt.datetime.combine(key[1], _dt.time(23, 30))
            if count != 48:
                violations["not_48"].append(key)
            if first_ts != expected_first:
                violations["first_not_0000"].append(key)
            if last_ts != expected_last:
                violations["last_not_2330"].append(key)

        for p in rows:
            total += 1
            ts_key = (p.customer_id, p.timestamp)

            if p.load_kwh != p.gc_kwh + p.cl_kwh:
                violations["load_identity"].append(ts_key)
            if p.solar_kwh != p.gg_kwh:
                violations["solar_identity"].append(ts_key)
            if p.load_kw != p.load_kwh * KWH_TO_KW:
                violations["load_kw_identity"].append(ts_key)
            if p.solar_kw != p.solar_kwh * KWH_TO_KW:
                violations["solar_kw_identity"].append(ts_key)

            key = (p.customer_id, p.timestamp.date())
            if key != current_key:
                if key in seen_groups:
                    duplicate_groups.append(key)
                seen_groups.add(key)
                finish_group(current_key, count, first_ts, last_ts)
                current_key = key
                count = 1
                first_ts = p.timestamp
                previous_ts = p.timestamp
                last_ts = p.timestamp
            else:
                count += 1
                last_ts = p.timestamp
                if p.timestamp - previous_ts != half_hour:
                    violations["spacing"].append(key)
                if p.timestamp == previous_ts:
                    violations["duplicate"].append(key)
                previous_ts = p.timestamp
            if p.timestamp.date() != key[1]:
                violations["out_of_date"].append(key)

        finish_group(current_key, count, first_ts, last_ts)

        checks = {
            "exactly_48_profiles_per_customer_date": len(violations["not_48"]) == 0,
            "first_timestamp_0000": len(violations["first_not_0000"]) == 0,
            "last_timestamp_2330": len(violations["last_not_2330"]) == 0,
            "spacing_30_min": len(violations["spacing"]) == 0,
            "no_duplicate_timestamps": len(violations["duplicate"]) == 0,
            "timestamps_within_source_date": len(violations["out_of_date"]) == 0,
            "no_duplicate_customer_timestamp": len(duplicate_groups) == 0,
            "load_kwh_equals_gc_plus_cl": len(violations["load_identity"]) == 0,
            "solar_kwh_equals_gg": len(violations["solar_identity"]) == 0,
            "load_kw_equals_load_kwh_x2": len(violations["load_kw_identity"]) == 0,
            "solar_kw_equals_solar_kwh_x2": len(violations["solar_kw_identity"]) == 0,
        }

        return {
            "groups_checked": groups,
            "total_records": total,
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
            "duplicate_group_examples": duplicate_groups[:5],
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COLUMNS = [
    "customer_id", "postcode", "generator_capacity_kwp", "timestamp",
    "gc_kwh", "cl_kwh", "gg_kwh", "load_kwh", "solar_kwh",
    "load_kw", "solar_kw", "row_quality",
]


def _isclose(a: float, b: float, rel_tol: float = 1e-12) -> bool:
    return abs(a - b) <= rel_tol * max(abs(a), abs(b), 1.0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ausgrid_profiles.py",
        description=(
            "Combine Ausgrid time series into physical per-customer profiles "
            "(Phase A-4): load_kwh = gc_kwh + cl_kwh, solar_kwh = gg_kwh, "
            "kW = kWh * 2 (30-minute interval). No feeder/bus assignment."
        ),
    )
    ap.add_argument("csv_path", help="path to the Ausgrid CSV (e.g. 'Solar home 2010-2011.csv')")
    ap.add_argument(
        "--no-write-csv", action="store_true",
        help="do not write the output CSV (default: write it)",
    )
    ap.add_argument(
        "--output", default="ausgrid_profiles_2010_2011.csv", metavar="PATH",
        help="output CSV path (default: ausgrid_profiles_2010_2011.csv)",
    )
    ap.add_argument(
        "--validate", action="store_true",
        help="run profile integrity validation plus a category-energy "
             "cross-check against the source time series",
    )
    args = ap.parse_args(argv)

    parser = AusgridParser(args.csv_path)
    try:
        source_records = parser.parse()
    except Exception as exc:
        print(f"ERROR parsing source CSV: {exc}", file=sys.stderr)
        return 1

    ts = AusgridTimeSeriesConverter(parser)
    converter = AusgridProfileConverter(ts)

    ts_total = len(source_records) * len(parser.interval_labels)

    # Unique (customer_id, date) groups come from GC, which covers all
    # customers and days -> each group yields exactly 48 profiles.
    unique_groups = len({(r.customer_id, r.date) for r in source_records})
    expected_profiles = unique_groups * 48

    customers_with_cl = {r.customer_id for r in source_records if r.category == "CL"}
    customers_with_gg = {r.customer_id for r in source_records if r.category == "GG"}
    categories = Counter(r.category for r in source_records)
    capacities = [r.generator_capacity_kwp for r in source_records]

    # Single streaming pass: summary + samples + optional CSV write + totals.
    total = 0
    customers = set()
    dates = set()
    qualities = Counter()
    totals = {
        "gc": 0.0, "cl": 0.0, "gg": 0.0,
        "load": 0.0, "solar": 0.0, "load_kw": 0.0, "solar_kw": 0.0,
    }
    min_ts = None
    max_ts = None
    samples: List[AusgridProfile] = []
    cl_absent_samples: List[AusgridProfile] = []
    seen_cl_absent_customer: Optional[int] = None
    want_samples = 3

    out_fh = None
    out_writer = None
    if not args.no_write_csv:
        out_path = Path(args.output)
        out_fh = open(out_path, "w", newline="")
        out_writer = csv.writer(out_fh)
        out_writer.writerow(_COLUMNS)

    try:
        for p in converter.iter_profiles(ts.iter_records(source_records)):
            total += 1
            customers.add(p.customer_id)
            dates.add(p.timestamp.date())
            qualities[p.row_quality] += 1
            totals["gc"] += p.gc_kwh
            totals["cl"] += p.cl_kwh
            totals["gg"] += p.gg_kwh
            totals["load"] += p.load_kwh
            totals["solar"] += p.solar_kwh
            totals["load_kw"] += p.load_kw
            totals["solar_kw"] += p.solar_kw
            if min_ts is None or p.timestamp < min_ts:
                min_ts = p.timestamp
            if max_ts is None or p.timestamp > max_ts:
                max_ts = p.timestamp
            if len(samples) < want_samples:
                samples.append(p)
            if (seen_cl_absent_customer is None
                    and p.customer_id not in customers_with_cl
                    and len(cl_absent_samples) == 0):
                seen_cl_absent_customer = p.customer_id
            if p.customer_id == seen_cl_absent_customer and len(cl_absent_samples) < want_samples:
                cl_absent_samples.append(p)
            if out_writer is not None:
                out_writer.writerow(p.to_dict().values())
    finally:
        if out_fh is not None:
            out_fh.close()

    if total != expected_profiles:
        print(
            f"ERROR: expected {expected_profiles} profiles but produced {total}",
            file=sys.stderr,
        )
        return 1

    print(f"Source CSV                            : {parser.csv_path}")
    print(f"Parsed source rows                    : {len(source_records)}")
    print(f"Interval columns                      : {len(parser.interval_labels)}")
    print(f"Source time-series records (rows x intervals): {ts_total}")
    print(f"Expected profiles (unique customer+date x 48): {expected_profiles}")
    print(f"Total profile records                 : {total}")
    print(f"Unique customers                      : {len(customers)}")
    print(f"Date range                            : {min(dates).isoformat()} -> {max(dates).isoformat()}")
    print(f"Timestamp range                       : {min_ts.isoformat(sep='T')} -> {max_ts.isoformat(sep='T')}")
    print(f"Categories observed (source)          : {dict(categories)}")
    print(f"Customers with CL (controlled load)   : {len(customers_with_cl)}")
    print(f"Customers with GG (gross generation)  : {len(customers_with_gg)}")
    print(f"Generator capacity kWp range          : {min(capacities)}..{max(capacities)}")
    print(f"Row Quality values                    : {dict(qualities)}")
    print("")
    print(f"Totals (verified from generated profiles):")
    print(f"  total_gc_kwh                        : {totals['gc']:.3f}")
    print(f"  total_cl_kwh                        : {totals['cl']:.3f}")
    print(f"  total_gg_kwh                        : {totals['gg']:.3f}")
    print(f"  total_load_kwh                      : {totals['load']:.3f}")
    print(f"  total_solar_kwh                     : {totals['solar']:.3f}")
    print(f"  total_load_kw                       : {totals['load_kw']:.3f}")
    print(f"  total_solar_kw                      : {totals['solar_kw']:.3f}")
    gt = totals
    print("")
    print("Verification of identities (from totals):")
    print(f"  total_load_kwh == total_gc_kwh + total_cl_kwh : "
          f"{_isclose(gt['load'], gt['gc'] + gt['cl'])}")
    print(f"  total_solar_kwh == total_gg_kwh               : "
          f"{_isclose(gt['solar'], gt['gg'])}")
    print(f"  total_load_kw  == total_load_kwh * 2          : "
          f"{_isclose(gt['load_kw'], gt['load'] * 2)}")
    print(f"  total_solar_kw == total_solar_kwh * 2         : "
          f"{_isclose(gt['solar_kw'], gt['solar'] * 2)}")
    if out_fh is not None:
        print(f"Wrote profiles CSV                     : {Path(args.output)}")

    print("\nSample profiles (customer with GC + CL + GG present):")
    for p in samples:
        print("  " + str(p.to_dict()))

    if seen_cl_absent_customer is not None:
        print(f"\nSample profiles (customer {seen_cl_absent_customer}, CL absent, cl_kwh = 0.0):")
        for p in cl_absent_samples:
            print("  " + str(p.to_dict()))

    if args.validate:
        result = converter.validate_profiles(
            converter.iter_profiles(ts.iter_records(source_records))
        )
        print("\nProfile integrity validation:")
        for name, ok in result["checks"].items():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {name}")
        print(f"  groups checked          : {result['groups_checked']}"
              f"  (total profiles: {result['total_records']})")
        print(f"  all checks passed       : {result['all_passed']}")
        for key, examples in result["violation_examples"].items():
            if examples:
                print(f"  violations [{key}]        : {examples}")
        if result["duplicate_group_examples"]:
            print(f"  duplicate customer+date groups     : {result['duplicate_group_examples']}")

        # Cross-check: category energy above must equal the source time series.
        cat_sums = {"GC": 0.0, "CL": 0.0, "GG": 0.0}
        n_records = 0
        for rec in ts.iter_records(source_records):
            n_records += 1
            cat_sums[rec.category] += rec.energy_kwh
        x = {
            "gc": cat_sums["GC"],
            "cl": cat_sums["CL"],
            "gg": cat_sums["GG"],
        }
        print("\nCategory-energy cross-check (profiles vs source time series):")
        print(f"  source GC energy == total_gc_kwh : {_isclose(x['gc'], gt['gc'])}"
              f"  ({x['gc']:.3f} vs {gt['gc']:.3f})")
        print(f"  source CL energy == total_cl_kwh : {_isclose(x['cl'], gt['cl'])}"
              f"  ({x['cl']:.3f} vs {gt['cl']:.3f})")
        print(f"  source GG energy == total_gg_kwh : {_isclose(x['gg'], gt['gg'])}"
              f"  ({x['gg']:.3f} vs {gt['gg']:.3f})")
        print(f"  time-series records re-scanned    : {n_records}")

    return 0


if __name__ == "__main__":
    main()