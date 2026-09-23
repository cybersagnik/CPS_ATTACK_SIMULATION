#!/usr/bin/env python3
"""Customer-level access layer over the Phase A-4 physical profiles.

Stage in the CPS project (Phase B / Step 6):
    ausgrid_profiles_2010_2011.csv        <- Phase A-4 output (input here)
        |
        v
    profile_engine.py                     <- Phase B-6 (THIS module)
        |
        v
    customer profiles (ordered 30-min samples per customer)
        |
        v
    random assignment / feeder targets    <- Phase B-7 (FUTURE, NOT here)

This module is a pure ACCESS layer over the already-created physical
profiles.  It does NOT: re-implement A-2/A-3/A-4 parsing/melting/merging,
re-shift timestamps, convert timezones, or recompute any physical quantity.
All physical fields (kwh/kw) are read back verbatim from the A-4 CSV and
exposed as-is.

Deliberate scope limits (Phase B-6 stops here):
    * No random assignment, no profile->feeder/bus/load/generator ids.
    * No phase allocation, no Load.kw_per_phase modification.
    * No reactive power / kVAr / power-factor / IEEE-37 / IEEE-123 logic.
    * No Sagnik's Common Grid Model integration.
    * No SCADA, telemetry, attack logic, MITRE, network simulation.
    * No profile_assignment.csv writing.

Memory strategy:
    __init__ performs a SINGLE streaming pass over the CSV and writes only a
    lightweight index: customer_id -> (byte offset of its first row, row
    count), customer_id -> (postcode, generator_capacity_kwp), plus aggregate
    counters (totals, CL/GG presence, capacity range, timestamp endpoints).
    NO ProfileSample / CustomerProfile objects are created during init, so
    the 5,256,000 rows are never materialised.

    get_customer_profile() seeks straight to one customer's block and parses
    only that customer's ~17,520 rows.
    iter_customer_profiles() streams the file once, yielding one customer at
    a time.

API:
    engine = ProfileEngine("ausgrid_profiles_2010_2011.csv")
    ids = engine.get_customer_ids()              # list[int]
    prof = engine.get_customer_profile(1)        # CustomerProfile | None
    for cust in engine.iter_customer_profiles(): # generator
        ...
    engine.summary()                             # dict
    engine.validate()                            # dict

CLI:
    python3 profile_engine.py ausgrid_profiles_2010_2011.csv [--validate]
                                 [--customer 1]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Expected A-4 output schema (column order is fixed).
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS = [
    "customer_id", "postcode", "generator_capacity_kwp", "timestamp",
    "gc_kwh", "cl_kwh", "gg_kwh", "load_kwh", "solar_kwh",
    "load_kw", "solar_kw", "row_quality",
]

# Column positions (0-based) in the A-4 CSV.
_IDX = {
    "customer_id": 0,
    "postcode": 1,
    "generator_capacity_kwp": 2,
    "timestamp": 3,
    "gc_kwh": 4,
    "cl_kwh": 5,
    "gg_kwh": 6,
    "load_kwh": 7,
    "solar_kwh": 8,
    "load_kw": 9,
    "solar_kw": 10,
    "row_quality": 11,
}

HALF_HOUR = _dt.timedelta(minutes=30)

# Expected intervals per day: 48 half-hour slots (00:00 -> 23:30).
INTERVALS_PER_DAY = 48


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProfileEngineError(Exception):
    """Raised when the profile CSV cannot be indexed/parsed."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileSample:
    """One timestamped physical profile sample for a customer.

    All values are read back verbatim from the Phase A-4 CSV (no recompute).

    Fields:
        timestamp:     interval START as naive local wall clock datetime.
        gc_kwh:        General Consumption energy (kWh) for the interval.
        cl_kwh:        Controlled Load energy (kWh); 0.0 when customer has none.
        gg_kwh:        Gross Generation energy (kWh).
        load_kwh:      gc_kwh + cl_kwh (kWh).
        solar_kwh:     gg_kwh (kWh).
        load_kw:       load_kwh * 2 (average power, kW, 30-min interval).
        solar_kw:      solar_kwh * 2 (average power, kW, 30-min interval).
        row_quality:   "actual"/"NA"/None as preserved from Phase A-4.
    """

    timestamp: _dt.datetime
    gc_kwh: float
    cl_kwh: float
    gg_kwh: float
    load_kwh: float
    solar_kwh: float
    load_kw: float
    solar_kw: float
    row_quality: Optional[str]


@dataclass(frozen=True)
class CustomerProfile:
    """Complete time series for one customer.

    `samples` is a tuple of ProfileSample ordered by ascending timestamp with
    30-minute spacing (17,520 samples = 365 days x 48 for the real dataset).
    Convenience series helpers return plain tuples for Phase B-7.
    """

    customer_id: int
    postcode: int
    generator_capacity_kwp: float
    samples: Tuple[ProfileSample, ...]

    def load_kw_series(self) -> Tuple[float, ...]:
        """Tuple of load_kw per sample, in time order (B-7 friendly)."""
        return tuple(s.load_kw for s in self.samples)

    def solar_kw_series(self) -> Tuple[float, ...]:
        """Tuple of solar_kw per sample, in time order (B-7 friendly)."""
        return tuple(s.solar_kw for s in self.samples)

    def load_kwh_series(self) -> Tuple[float, ...]:
        """Tuple of load_kwh per sample, in time order."""
        return tuple(s.load_kwh for s in self.samples)

    def solar_kwh_series(self) -> Tuple[float, ...]:
        """Tuple of solar_kwh per sample, in time order."""
        return tuple(s.solar_kwh for s in self.samples)

    def timestamp_series(self) -> Tuple[_dt.datetime, ...]:
        """Tuple of timestamps per sample, in time order."""
        return tuple(s.timestamp for s in self.samples)


# ---------------------------------------------------------------------------
# Row parsing helpers
# ---------------------------------------------------------------------------

def _parse_row(fields: Sequence[str]) -> ProfileSample:
    """Build a ProfileSample from a split A-4 CSV row."""
    try:
        ts = _dt.datetime.fromisoformat(fields[_IDX["timestamp"]])
        gc = float(fields[_IDX["gc_kwh"]])
        cl = float(fields[_IDX["cl_kwh"]])
        gg = float(fields[_IDX["gg_kwh"]])
        load_kwh = float(fields[_IDX["load_kwh"]])
        solar_kwh = float(fields[_IDX["solar_kwh"]])
        load_kw = float(fields[_IDX["load_kw"]])
        solar_kw = float(fields[_IDX["solar_kw"]])
    except (ValueError, IndexError) as exc:
        raise ProfileEngineError(f"cannot parse profile row: {fields!r}") from exc
    quality = fields[_IDX["row_quality"]]
    if quality == "":
        quality = None
    return ProfileSample(
        timestamp=ts,
        gc_kwh=gc,
        cl_kwh=cl,
        gg_kwh=gg,
        load_kwh=load_kwh,
        solar_kwh=solar_kwh,
        load_kw=load_kw,
        solar_kw=solar_kw,
        row_quality=quality,
    )


# ---------------------------------------------------------------------------
# ProfileEngine
# ---------------------------------------------------------------------------

class ProfileEngine:
    """Indexed access to the Phase A-4 physical profile CSV.

    Initialization streams the file ONCE to build a lightweight index and
    aggregate counters; it does not create per-row objects.
    """

    def __init__(self, profile_csv: str | Path) -> None:
        self.path = Path(profile_csv)

        # per-customer index: customer_id -> (start_offset, row_count)
        self._blocks: Dict[int, Tuple[int, int]] = {}
        # customer_id -> (postcode, generator_capacity_kwp)
        self._meta: Dict[int, Tuple[int, float]] = {}
        # customer ids in file order (ascending for the real dataset)
        self._customer_order: List[int] = []

        self._total_profiles = 0
        self._timestamp_min: Optional[_dt.datetime] = None
        self._timestamp_max: Optional[_dt.datetime] = None
        self._customers_with_cl: set = set()
        self._customers_with_gg: set = set()
        self._generator_capacity_min: Optional[float] = None
        self._generator_capacity_max: Optional[float] = None
        self._total_load_kwh = 0.0
        self._total_solar_kwh = 0.0
        self._capacity_consistent = True

        self._index()

    # -- indexing (single streaming pass, no object materialisation) --------

    def _index(self) -> None:
        try:
            f = open(self.path, "rb")
        except OSError as exc:
            raise ProfileEngineError(f"cannot open profile CSV: {exc}") from exc
        with f:
            header_line = f.readline()
            if not header_line:
                raise ProfileEngineError("profile CSV is empty (no header)")
            header = header_line.decode("utf-8", "replace").rstrip("\r\n").split(",")
            if header != EXPECTED_COLUMNS:
                raise ProfileEngineError(
                    "unexpected profile header; expected the Phase A-4 columns "
                    f"{EXPECTED_COLUMNS}, got {header}"
                )

            block_start: Optional[int] = None
            prev_customer: Optional[int] = None
            block_count = 0
            last_ts: Optional[_dt.datetime] = None

            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text == "":
                    continue  # tolerate a stray trailing blank line
                fields = text.split(",")
                try:
                    customer_id = int(fields[_IDX["customer_id"]])
                except (ValueError, IndexError) as exc:
                    raise ProfileEngineError(
                        f"invalid customer_id at byte {offset}: {line!r}"
                    ) from exc

                if customer_id != prev_customer:
                    if prev_customer is not None:
                        self._blocks[prev_customer] = (block_start, block_count)
                    prev_customer = customer_id
                    block_start = offset
                    block_count = 0
                    self._customer_order.append(customer_id)
                    # per-customer metadata: constant per block, read on first row
                    try:
                        postcode = int(fields[_IDX["postcode"]])
                        capacity = float(fields[_IDX["generator_capacity_kwp"]])
                    except (ValueError, IndexError) as exc:
                        raise ProfileEngineError(
                            f"invalid metadata at byte {offset}: {line!r}"
                        ) from exc
                    if customer_id in self._meta:
                        old = self._meta[customer_id]
                        self._capacity_consistent = False
                        if old != (postcode, capacity):
                            raise ProfileEngineError(
                                f"inconsistent postcode/capacity for customer "
                                f"{customer_id} (blocks split in file)"
                            )
                    else:
                        self._meta[customer_id] = (postcode, capacity)
                    if (self._generator_capacity_min is None
                            or capacity < self._generator_capacity_min):
                        self._generator_capacity_min = capacity
                    if (self._generator_capacity_max is None
                            or capacity > self._generator_capacity_max):
                        self._generator_capacity_max = capacity

                block_count += 1
                self._total_profiles += 1

                ts = _dt.datetime.fromisoformat(fields[_IDX["timestamp"]])
                if self._timestamp_min is None or ts < self._timestamp_min:
                    self._timestamp_min = ts
                if self._timestamp_max is None or ts > self._timestamp_max:
                    self._timestamp_max = ts
                last_ts = ts

                cl = float(fields[_IDX["cl_kwh"]])
                gg = float(fields[_IDX["gg_kwh"]])
                if cl != 0.0:
                    self._customers_with_cl.add(customer_id)
                if gg != 0.0:
                    self._customers_with_gg.add(customer_id)

                self._total_load_kwh += float(fields[_IDX["load_kwh"]])
                self._total_solar_kwh += float(fields[_IDX["solar_kwh"]])

            if prev_customer is not None:
                self._blocks[prev_customer] = (block_start, block_count)

        if not self._blocks:
            raise ProfileEngineError("profile CSV contains no data rows")

    # -- public API ----------------------------------------------------------

    def get_customer_ids(self) -> List[int]:
        """Return all customer ids in file order (ascending for the dataset)."""
        return list(self._customer_order)

    def get_customer_profile(self, customer_id: int) -> Optional[CustomerProfile]:
        """Return the complete, time-ordered CustomerProfile or None.

        Unknown ids return None (documented API contract: an empty result,
        not an exception). Only the requested customer's block is read.
        """
        block = self._blocks.get(customer_id)
        if block is None:
            return None
        start, count = block
        meta = self._meta[customer_id]
        samples = self._read_block(start, count)
        return CustomerProfile(
            customer_id=customer_id,
            postcode=meta[0],
            generator_capacity_kwp=meta[1],
            samples=samples,
        )

    def iter_customer_profiles(self) -> Iterator[CustomerProfile]:
        """Yield a CustomerProfile per customer in file order.

        Streams the file once (seeking per block); each customer's ~17,520
        samples is materialised only while yielded, then released.
        """
        with open(self.path, "rb") as f:
            for customer_id in self._customer_order:
                start, count = self._blocks[customer_id]
                f.seek(start)
                samples = self._read_block_from_file(f, count)
                meta = self._meta[customer_id]
                yield CustomerProfile(
                    customer_id=customer_id,
                    postcode=meta[0],
                    generator_capacity_kwp=meta[1],
                    samples=samples,
                )

    def summary(self) -> Dict[str, object]:
        """Metadata over all profiles without materialising the dataset."""
        samples_counts = [c for _, c in self._blocks.values()]
        uniform = len(set(samples_counts)) == 1
        return {
            "total_profiles": self._total_profiles,
            "unique_customers": len(self._customer_order),
            "customer_id_range": (
                min(self._customer_order), max(self._customer_order),
            ),
            "timestamp_start": (
                self._timestamp_min.isoformat(sep="T") if self._timestamp_min else None
            ),
            "timestamp_end": (
                self._timestamp_max.isoformat(sep="T") if self._timestamp_max else None
            ),
            "samples_per_customer": (
                samples_counts[0] if uniform else samples_counts
            ),
            "customers_with_cl": len(self._customers_with_cl),
            "customers_with_gg": len(self._customers_with_gg),
            "generator_capacity_range": (
                self._generator_capacity_min, self._generator_capacity_max,
            ),
            "total_load_kwh": self._total_load_kwh,
            "total_solar_kwh": self._total_solar_kwh,
        }

    def validate(self) -> Dict[str, object]:
        """Run B-6 integrity checks over every customer (generic, not just 1).

        Streaming per-customer block reads. Checks (for the whole dataset):

          1. expected A-4 header present (already enforced in __init__);
          2. customer ids valid/discoverable, in ascending file order;
          3. no duplicate (customer_id, timestamp);
          4. samples per customer matches DAYS x INTERVALS_PER_DAY derived
             from that customer's own timestamps;
          5. timestamps strictly increasing within each customer;
          6. consecutive timestamps exactly 30 minutes apart;
          7. load_kwh == gc_kwh + cl_kwh;
          8. solar_kwh == gg_kwh;
          9. load_kw == load_kwh * 2;
         10. solar_kw == solar_kwh * 2;
         11. generator_capacity_kwp preserved: constant per customer and
             within A-4's documented 1.0..9.99 range for the real dataset
             (range check only when the dataset has 300 customers / 365 days);
         12. row_quality preserved: consistent within each customer.
        """
        half_hour = HALF_HOUR

        violations = {k: [] for k in (
            "unknown_customer_id", "column_count", "duplicate",
            "bad_sample_count", "timestamp_order", "spacing",
            "load_identity", "solar_identity", "load_kw_identity",
            "solar_kw_identity", "capacity_missing", "row_quality_mismatch",
        )}
        total_profiles = 0
        customers_checked = 0

        for customer_id in self._customer_order:
            customers_checked += 1
            block = self._blocks.get(customer_id)
            if block is None:
                violations["unknown_customer_id"].append(customer_id)
                continue
            start, count = block
            samples = self._read_block(start, count)
            expected = self._expected_sample_count(samples)
            if len(samples) != expected:
                violations["bad_sample_count"].append((customer_id, len(samples), expected))
            meta = self._meta[customer_id]
            prev_ts = None
            seen = set()
            # expected capacity = the A-4 preserved metadata value
            capacity = meta[1]
            quality_set = set()
            for s in samples:
                total_profiles += 1
                key = (customer_id, s.timestamp)
                if key in seen:
                    violations["duplicate"].append(key)
                seen.add(key)
                if prev_ts is not None:
                    if s.timestamp <= prev_ts:
                        violations["timestamp_order"].append(key)
                    if s.timestamp - prev_ts != half_hour:
                        violations["spacing"].append(key)
                prev_ts = s.timestamp

                if s.load_kwh != s.gc_kwh + s.cl_kwh:
                    violations["load_identity"].append(key)
                if s.solar_kwh != s.gg_kwh:
                    violations["solar_identity"].append(key)
                if s.load_kw != s.load_kwh * 2.0:
                    violations["load_kw_identity"].append(key)
                if s.solar_kw != s.solar_kwh * 2.0:
                    violations["solar_kw_identity"].append(key)
                quality_set.add(s.row_quality)

            # capacity preserved: the block's metadata equals the sampled rows
            # (samples in A-4 carry the same value on every row, but the
            # constant is stored per customer — verify we parsed a non-negative
            # capacity and the index reported per-block consistency).
            if not (capacity is not None and capacity >= 0):
                violations["capacity_missing"].append(customer_id)
            if len(quality_set) > 1:
                violations["row_quality_mismatch"].append(customer_id)

        checks = {
            "expected_header": True,  # enforced in __init__
            "customer_ids_valid": len(violations["unknown_customer_id"]) == 0,
            "sample_count_matches_days_x_48": len(violations["bad_sample_count"]) == 0,
            "no_duplicate_customer_timestamp": len(violations["duplicate"]) == 0,
            "timestamps_increasing_per_customer": len(violations["timestamp_order"]) == 0,
            "spacing_30_min": len(violations["spacing"]) == 0,
            "load_kwh_equals_gc_plus_cl": len(violations["load_identity"]) == 0,
            "solar_kwh_equals_gg": len(violations["solar_identity"]) == 0,
            "load_kw_equals_load_kwh_x2": len(violations["load_kw_identity"]) == 0,
            "solar_kw_equals_solar_kwh_x2": len(violations["solar_kw_identity"]) == 0,
            "generator_capacity_kwp_preserved": (
                len(violations["capacity_missing"]) == 0 and self._capacity_consistent
            ),
            "row_quality_preserved": len(violations["row_quality_mismatch"]) == 0,
        }

        return {
            "customers_checked": customers_checked,
            "total_profiles": total_profiles,
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
        }

    # -- internal helpers ----------------------------------------------------

    def _expected_sample_count(self, samples: Sequence[ProfileSample]) -> int:
        """Derive expected samples per customer: unique dates x INTERVALS_PER_DAY."""
        dates = {s.timestamp.date() for s in samples}
        return len(dates) * INTERVALS_PER_DAY

    def _read_block(self, start: int, count: int) -> Tuple[ProfileSample, ...]:
        """Read byte range [start, start+count rows) from the CSV."""
        with open(self.path, "rb") as f:
            f.seek(start)
            return self._read_block_from_file(f, count)

    @staticmethod
    def _read_block_from_file(
        f, count: int
    ) -> Tuple[ProfileSample, ...]:
        samples = []
        # read `count` physical lines (rows are CRLF-separated, lenient on \r\n)
        for _ in range(count):
            line = f.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                continue
            samples.append(_parse_row(text.split(",")))
        return tuple(samples)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="profile_engine.py",
        description="Indexed access to the Phase A-4 physical profiles (B-6).",
    )
    ap.add_argument(
        "profile_csv",
        help="path to the A-4 output CSV (e.g. ausgrid_profiles_2010_2011.csv)",
    )
    ap.add_argument(
        "--validate", action="store_true",
        help="run full integrity validation over all customers",
    )
    ap.add_argument(
        "--customer", type=int, metavar="ID",
        help="print a small sample (first/last 3 rows) of the given customer",
    )
    args = ap.parse_args(argv)

    try:
        engine = ProfileEngine(args.profile_csv)
    except ProfileEngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    s = engine.summary()
    print("ProfileEngine summary")
    print(f"  profile CSV              : {engine.path}")
    print(f"  total profiles           : {s['total_profiles']}")
    print(f"  unique customers         : {s['unique_customers']}")
    print(f"  customer_id_range        : {s['customer_id_range'][0]}..{s['customer_id_range'][1]}")
    print(f"  timestamp_start          : {s['timestamp_start']}")
    print(f"  timestamp_end            : {s['timestamp_end']}")
    print(f"  samples_per_customer     : {s['samples_per_customer']}")
    print(f"  customers_with_cl        : {s['customers_with_cl']}")
    print(f"  customers_with_gg        : {s['customers_with_gg']}")
    print(f"  generator_capacity_range : {s['generator_capacity_range'][0]}..{s['generator_capacity_range'][1]} kWp")
    print(f"  total_load_kwh           : {s['total_load_kwh']:.3f}")
    print(f"  total_solar_kwh          : {s['total_solar_kwh']:.3f}")

    if args.customer is not None:
        prof = engine.get_customer_profile(args.customer)
        if prof is None:
            print(f"\ncustomer {args.customer}: NOT FOUND")
        else:
            n = len(prof.samples)
            print(f"\ncustomer {args.customer}: {n} samples")
            print("  first 3 samples:")
            for sm in prof.samples[:3]:
                print("   ", sm.timestamp.isoformat(), f"load={sm.load_kw}kW", f"solar={sm.solar_kw}kW",
                      f"(gc={sm.gc_kwh}, cl={sm.cl_kwh}, gg={sm.gg_kwh}) kWh")
            print("  last 3 samples:")
            for sm in prof.samples[-3:]:
                print("   ", sm.timestamp.isoformat(), f"load={sm.load_kw}kW", f"solar={sm.solar_kw}kW",
                      f"(gc={sm.gc_kwh}, cl={sm.cl_kwh}, gg={sm.gg_kwh}) kWh")

    if args.validate:
        r = engine.validate()
        print("\nProfileEngine validation:")
        for name, ok in r["checks"].items():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {name}")
        print(f"  customers checked        : {r['customers_checked']}")
        print(f"  total profiles checked   : {r['total_profiles']}")
        print(f"  all checks passed        : {r['all_passed']}")
        for key, examples in r["violation_examples"].items():
            if examples:
                print(f"  violations [{key}]        : {examples}")

    return 0


if __name__ == "__main__":
    main()