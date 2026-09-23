#!/usr/bin/env python3
"""Convert wide-format Ausgrid records into timestamp-based time series.

Stage in the CPS project (Phase A / Step 3):
    Ausgrid CSV -> ausgrid_parser.py -> structured source records
                                       -> ausgrid_timeseries.py
                                       -> timestamp-based time-series records

This module does NOT re-implement CSV parsing.  It consumes the validated
records produced by :mod:`ausgrid_parser` and "melts" each wide source row
into one output record per half-hour interval:

    source row (customer, postcode, capacity, category, date,
                {0:30: kwh, 1:00: kwh, ..., 0:00: kwh}, row_quality)
        -->
    48 output records, each with a single (timestamp, energy_kwh) pair.

Timestamp semantics (per the Ausgrid notes):
    The 48 interval columns label the END of each half-hour window.
        0:30 = energy used between 00:00 and 00:30
        1:00 = energy used between 00:30 and 01:00
        ...
        0:00 = energy used between 23:30 and 00:00
    Each output timestamp is therefore the START of its interval:
        0:30 -> 00:00
        1:00 -> 00:30
        ...
        0:00 -> 23:30
    The final "0:00" interval maps to 23:30 on the source date, never to the
    next day's 00:00.

Unit handling:
    The interval values are ENERGY in kWh.  This module preserves them as
    energy_kwh and does NOT convert kWh -> kW (that is Phase A-4).  The rated
    generator capacity is preserved as generator_capacity_kwp (kWp).

Row Quality:
    The 2010-2011 CSV contains no Row Quality column, so the parser reports
    row_quality = None.  This module preserves the parser's value verbatim
    (None here; "actual"/"NA" for datasets that include the column).  It never
    manufactures an "actual" value.

Output records are produced lazily via a generator so that the long format
(269735 source rows x 48 intervals = 12,947,280 records) is not needlessly
materialised in memory when a caller only wants to stream or count them.

API:
    converter = AusgridTimeSeriesConverter(AusgridParser("Solar home 2010-2011.csv"))
    for rec in converter.iter_records():   # generator, memory-efficient
        ...
    records = converter.convert()          # list[AusgridTimeSeriesRecord]

CLI:
    python3 ausgrid_timeseries.py "Solar home 2010-2011.csv"
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

from ausgrid_parser import AusgridParser, AusgridRecord

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AusgridTimeSeriesError(Exception):
    """Raised when a source record cannot be converted to time series."""


# ---------------------------------------------------------------------------
# Interval timestamp mapping
# ---------------------------------------------------------------------------

# An interval label is "H:MM" and identifies the END of a half-hour window.
_INTERVAL_LABEL_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")
_HALF_HOUR = _dt.timedelta(minutes=30)


def interval_start_time(label: str) -> _dt.time:
    """Return the START time of the half-hour interval whose END is `label`.

    Examples:
        interval_start_time("0:30") -> time(0, 0)
        interval_start_time("1:00") -> time(0, 30)
        interval_start_time("0:00") -> time(23, 30)   # 23:30 -> next-day 00:00
    """
    m = _INTERVAL_LABEL_RE.match(label)
    if m is None:
        raise ValueError(f"not an Ausgrid interval label: {label!r}")
    end_hour, end_minute = int(m.group(1)), int(m.group(2))
    end_dt = _dt.datetime.combine(_dt.date(2000, 1, 1), _dt.time(end_hour, end_minute))
    start_dt = end_dt - _HALF_HOUR
    return start_dt.time()


def interval_label_to_start_times(labels: Sequence[str]) -> List[_dt.time]:
    """Map every interval label to its interval-START clock time.

    The order mirrors the order of `labels` (i.e. the source CSV column order).
    """
    return [interval_start_time(lab) for lab in labels]


# ---------------------------------------------------------------------------
# Output record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AusgridTimeSeriesRecord:
    """One timestamped energy sample for a customer/category.

    Fields:
        customer_id:            integer customer identifier.
        postcode:               integer postcode.
        generator_capacity_kwp: rated solar capacity in kWp (preserved).
        category:               "GC", "CL" or "GG".
        date:                   source calendar date (datetime.date).
        timestamp:              interval START as local wall clock datetime.
        energy_kwh:             interval ENERGY in kWh (never converted to kW).
        row_quality:            "actual"/"NA" when the source had the column,
                                else None (unavailable).
    """

    customer_id: int
    postcode: int
    generator_capacity_kwp: float
    category: str
    date: _dt.date
    timestamp: _dt.datetime
    energy_kwh: float
    row_quality: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict view of the record (handy for debugging/export)."""
        return {
            "customer_id": self.customer_id,
            "postcode": self.postcode,
            "generator_capacity_kwp": self.generator_capacity_kwp,
            "category": self.category,
            "date": self.date.isoformat(),
            "timestamp": self.timestamp.isoformat(sep="T"),
            "energy_kwh": self.energy_kwh,
            "row_quality": self.row_quality,
        }


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

class AusgridTimeSeriesConverter:
    """Melt wide Ausgrid records into timestamp-based time series.

    The converter takes either a prepared :class:`AusgridParser` (recommended,
    no parsing is duplicated) or a CSV path from which it will build one:

        converter = AusgridTimeSeriesConverter(parser)
        converter = AusgridTimeSeriesConverter("Solar home 2010-2011.csv")
    """

    def __init__(self, source: AusgridParser | str | Path) -> None:
        if isinstance(source, AusgridParser):
            self.parser = source
        else:
            self.parser = AusgridParser(Path(source))
        # map interval label -> interval START clock time, in CSV column order
        self._start_times: Dict[str, _dt.time] = {
            lab: start for lab, start in zip(
                self.parser.interval_labels,
                interval_label_to_start_times(self.parser.interval_labels),
            )
        }

    # -- public API ---------------------------------------------------------

    def iter_records(
        self, records: Sequence[AusgridRecord] | None = None
    ) -> Iterator[AusgridTimeSeriesRecord]:
        """Yield one :class:`AusgridTimeSeriesRecord` per interval (generator).

        Records are produced lazily.  If `records` is not given, the source
        CSV is parsed on the fly; otherwise the given parsed records are used
        (useful when the caller has already called parser.parse()).
        """
        src_records = records if records is not None else self.parser.parse()
        labels = self.parser.interval_labels
        if len(self._start_times) != len(labels):
            raise AusgridTimeSeriesError(
                "interval label / start-time mapping is inconsistent"
            )

        for src in src_records:
            if not isinstance(src, AusgridRecord):
                raise AusgridTimeSeriesError(
                    "expected AusgridRecord instances from ausgrid_parser"
                )
            if len(src.intervals) != len(labels):
                raise AusgridTimeSeriesError(
                    f"line {src.line_number}: source row has {len(src.intervals)} "
                    f"intervals, expected {len(labels)}"
                )
            for label, kwh in src.intervals.items():
                start = self._start_times[label]
                timestamp = _dt.datetime.combine(src.date, start)
                yield AusgridTimeSeriesRecord(
                    customer_id=src.customer_id,
                    postcode=src.postcode,
                    generator_capacity_kwp=src.generator_capacity_kwp,
                    category=src.category,
                    date=src.date,
                    timestamp=timestamp,
                    energy_kwh=kwh,
                    row_quality=src.row_quality,
                )

    def convert(
        self, records: Sequence[AusgridRecord] | None = None
    ) -> List[AusgridTimeSeriesRecord]:
        """Materialise all time-series records into a list.

        Prefer iter_records() when only streaming/counting is needed: the full
        list for 2010-2011 holds ~12.95 million records.
        """
        return list(self.iter_records(records))

    @staticmethod
    def summarize(
        rows: Iterable[AusgridTimeSeriesRecord],
    ) -> Dict[str, object]:
        """Single-pass statistics over converted time-series rows.

        All numbers are computed from the actually converted data.
        """
        total = 0
        customers = set()
        categories = Counter()
        dates = set()
        qualities = Counter()
        min_ts = None
        max_ts = None
        for r in rows:
            total += 1
            customers.add(r.customer_id)
            categories[r.category] += 1
            dates.add(r.date)
            qualities[r.row_quality] += 1
            if min_ts is None or r.timestamp < min_ts:
                min_ts = r.timestamp
            if max_ts is None or r.timestamp > max_ts:
                max_ts = r.timestamp
        return {
            "total_records": total,
            "unique_customers": len(customers),
            "categories": dict(categories),
            "date_min": min(dates).isoformat() if dates else None,
            "date_max": max(dates).isoformat() if dates else None,
            "timestamp_min": min_ts.isoformat(sep="T") if min_ts else None,
            "timestamp_max": max_ts.isoformat(sep="T") if max_ts else None,
            "row_quality_values": dict(qualities),
        }

    @staticmethod
    def validate_timestamps(
        rows: Iterable[AusgridTimeSeriesRecord],
    ) -> Dict[str, object]:
        """Integrity validation of the converted time-series timestamps.

        For every (customer_id, category, date) combination this verifies:

          1. exactly 48 records exist;
          2. the first timestamp is 00:00 on the source date;
          3. the last timestamp is 23:30 on the source date;
          4. consecutive timestamps are exactly 30 minutes apart;
          5. no duplicate timestamps exist;
          6. no timestamp falls outside the source date.

        The check is streaming: it keeps constant state per group and relies on
        iter_records() emitting each (customer_id, category, date) group
        contiguously (48 consecutive records), which holds for the deterministic
        source-row order this converter produces.  Timestamp semantics are not
        changed here and no kWh -> kW conversion is performed.

        Returns a dict with the number of groups/records examined, one
        ok/failed block per check, and the offending group keys (first few).
        """
        half_hour = _dt.timedelta(minutes=30)

        def initial_timestamp(day: _dt.date, hour: int, minute: int) -> _dt.datetime:
            return _dt.datetime.combine(day, _dt.time(hour, minute))

        groups = 0
        total = 0

        # violation lists: each entry is a (customer_id, category, date) key
        violations = {
            "not_48": [],
            "first_not_0000": [],
            "last_not_2330": [],
            "spacing": [],
            "duplicate": [],
            "out_of_date": [],
        }

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
            expected_first = initial_timestamp(key[2], 0, 0)
            expected_last = initial_timestamp(key[2], 23, 30)
            if count != 48:
                violations["not_48"].append(key)
            if first_ts != expected_first:
                violations["first_not_0000"].append(key)
            if last_ts != expected_last:
                violations["last_not_2330"].append(key)

        for r in rows:
            total += 1
            key = (r.customer_id, r.category, r.date)
            if key != current_key:
                finish_group(current_key, count, first_ts, last_ts)
                current_key = key
                count = 1
                first_ts = r.timestamp
                previous_ts = r.timestamp
                last_ts = r.timestamp
            else:
                count += 1
                last_ts = r.timestamp
                if r.timestamp - previous_ts != half_hour:
                    violations["spacing"].append(key)
                if r.timestamp == previous_ts:
                    violations["duplicate"].append(key)
                previous_ts = r.timestamp
            # every timestamp must fall on the source date
            if r.timestamp.date() != r.date:
                violations["out_of_date"].append(key)

        finish_group(current_key, count, first_ts, last_ts)

        checks = {
            "exactly_48_records": len(violations["not_48"]) == 0,
            "first_timestamp_0000": len(violations["first_not_0000"]) == 0,
            "last_timestamp_2330": len(violations["last_not_2330"]) == 0,
            "spacing_30_min": len(violations["spacing"]) == 0,
            "no_duplicate_timestamps": len(violations["duplicate"]) == 0,
            "timestamps_within_source_date": len(violations["out_of_date"]) == 0,
        }

        return {
            "groups_checked": groups,
            "total_records": total,
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ausgrid_timeseries.py",
        description=(
            "Convert wide-format Ausgrid parsed records into timestamp-based "
            "time series (Phase A-3). kWh is NOT converted to kW."
        ),
    )
    ap.add_argument("csv_path", help="path to the Ausgrid CSV (e.g. 'Solar home 2010-2011.csv')")
    ap.add_argument(
        "--no-write-csv", action="store_true",
        help="do not write the output CSV (default: write it)",
    )
    ap.add_argument(
        "--output", default="ausgrid_timeseries_2010_2011.csv", metavar="PATH",
        help="output CSV path (default: ausgrid_timeseries_2010_2011.csv)",
    )
    ap.add_argument(
        "--validate", action="store_true",
        help="run the timestamp integrity validation and print a summary",
    )
    args = ap.parse_args(argv)

    parser = AusgridParser(args.csv_path)
    try:
        source_records = parser.parse()
    except Exception as exc:
        print(f"ERROR parsing source CSV: {exc}", file=sys.stderr)
        return 1

    converter = AusgridTimeSeriesConverter(parser)
    expected = len(source_records) * len(parser.interval_labels)

    # Single streaming pass: build summary + samples + optionally write CSV.
    total = 0
    customers = set()
    categories = Counter()
    dates = set()
    qualities = Counter()
    min_ts = None
    max_ts = None
    samples: List[AusgridTimeSeriesRecord] = []
    want_samples = 3
    out_fh = None
    out_writer = None
    if not args.no_write_csv:
        out_path = Path(args.output)
        out_fh = open(out_path, "w", newline="")
        out_writer = csv.writer(out_fh)
        out_writer.writerow(
            ["customer_id", "postcode", "generator_capacity_kwp",
             "category", "date", "timestamp", "energy_kwh", "row_quality"]
        )

    try:
        for rec in converter.iter_records(source_records):
            total += 1
            customers.add(rec.customer_id)
            categories[rec.category] += 1
            dates.add(rec.date)
            qualities[rec.row_quality] += 1
            if min_ts is None or rec.timestamp < min_ts:
                min_ts = rec.timestamp
            if max_ts is None or rec.timestamp > max_ts:
                max_ts = rec.timestamp
            if len(samples) < want_samples:
                samples.append(rec)
            if out_writer is not None:
                out_writer.writerow(rec.to_dict().values())
    finally:
        if out_fh is not None:
            out_fh.close()

    if total != expected:
        print(
            f"ERROR: expected {expected} time-series records but produced {total}",
            file=sys.stderr,
        )
        return 1

    print(f"Source CSV                            : {parser.csv_path}")
    print(f"Parsed source rows                    : {len(source_records)}")
    print(f"Interval columns                      : {len(parser.interval_labels)}")
    print(f"Expected time-series records (rows x intervals): {expected}")
    print(f"Total time-series records             : {total}")
    print(f"Unique customers                      : {len(customers)}")
    print(f"Categories                            : {dict(categories)}")
    print(f"Date range                            : {min(dates).isoformat()} -> {max(dates).isoformat()}")
    print(f"Timestamp range                       : {min_ts.isoformat(sep='T')} -> {max_ts.isoformat(sep='T')}")
    print(f"Row Quality values                    : {dict(qualities)}")
    if out_fh is not None:
        print(f"Wrote time-series CSV                 : {Path(args.output)}")

    print("\nSample converted records:")
    for rec in samples:
        print("  " + str(rec.to_dict()))

    # Timestamp mapping demonstration for the first source row (customer 1, GC).
    if source_records:
        first = source_records[0]
        print(f"\nInterval START mapping (first source row, customer {first.customer_id}, {first.date}):")
        for label, kwh in first.intervals.items():
            start = converter._start_times[label]
            print(f"  {label:<5} -> {start.isoformat()}   ({kwh!r} kWh)")
        last_label = list(first.intervals)[-1]
        print(f"  confirmed: last interval {last_label!r} -> {converter._start_times[last_label]}")

    if args.validate:
        result = converter.validate_timestamps(converter.iter_records(source_records))
        print("\nTimestamp integrity validation:")
        for name, ok in result["checks"].items():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {name}")
        print(f"  groups checked              : {result['groups_checked']}"
              f"  (total records: {result['total_records']})")
        print(f"  all checks passed           : {result['all_passed']}")
        for key, examples in result["violation_examples"].items():
            if examples:
                print(f"  violations [{key}]            : {examples}")

    return 0


if __name__ == "__main__":
    main()