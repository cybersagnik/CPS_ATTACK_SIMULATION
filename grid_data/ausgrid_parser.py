#!/usr/bin/env python3
"""Parse the Ausgrid "Solar home" CSV dataset into clean, validated records.

Stage in the CPS project (Phase A / Step 2):
    Ausgrid CSV -> ausgrid_parser.py -> structured records

The parser is deliberately decoupled from later pipeline stages (profile
processing, feeder assignment, simulation, attack engine, ...).  Its only job
is to turn the raw wide-format Ausgrid CSV into validated structured records.

Important: this implementation was written against the *actual* file
"Solar home 2010-2011.csv" after inspecting it.  The real file differs from
the general Ausgrid notes in several ways, which are handled here:

  * There is NO "Row Quality" column in the 2010-2011 file.  The header is
        Customer, Generator Capacity, Postcode, Consumption Category, date,
        followed by 48 half-hour interval columns.
    The parser detects whether a Row Quality column is present.  When it is
    absent, row_quality is set to None (unavailable) -- the parser does NOT
    infer or manufacture "actual" values that were never recorded.

  * Dates use the format "D-Mon-YY" (e.g. "1-Jul-10"), not "DDMMMYYYY".
    The parser accepts BOTH formats to stay robust to other releases.

  * Controlled Load (CL) rows are only present for a subset of customers
    (139 of the 300 in the 2010-2011 file).  This is real and expected: not
    every home has a controlled load.

  * Values in the 48 interval columns are ENERGY in kWh per half-hour
    (e.g. "0:30" = energy used/generated between 00:00 and 00:30).  This
    parser does NOT convert kWh -> kW.

This module has no third-party dependencies (Python standard library only).

API:
    parser = AusgridParser("Solar home 2010-2011.csv")
    records = parser.parse()           # list[AusgridRecord]
    parser.summarize(records)          # dict of statistics

CLI:
    python ausgrid_parser.py "Solar home 2010-2011.csv"
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import re
import sys
from collections import OrderedDict, Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_CATEGORIES = frozenset({"GC", "CL", "GG"})

# Human readable labels for the three consumption categories.
CATEGORY_LABELS = {
    "GC": "General Consumption",
    "CL": "Controlled Load Consumption",
    "GG": "Gross Generation",
}

ROW_QUALITY_ACTUAL = "actual"
ROW_QUALITY_NA = "NA"

# Value used when the CSV has no Row Quality column at all: the quality of
# each row is simply unknown.
ROW_QUALITY_UNAVAILABLE = None

# Names we look for when mapping the header (compared case-insensitively).
REQUIRED_HEADER_FIELDS = {
    "customer",
    "generator capacity",
    "postcode",
    "consumption category",
    "date",
}
ROW_QUALITY_HEADER_NAMES = ("row quality",)

# Half-hour interval columns are identified from the actual CSV header by
# matching time labels of the form "HH:MM" or "H:MM".  We expect exactly 48.
_INTERVAL_COLUMN_RE = re.compile(r"^\d{1,2}:\d{2}$")

_EXPECTED_INTERVAL_COUNT = 48

# Date formats we accept.  The 2010-2011 file uses "%-d-%b-%y" like "1-Jul-10";
# we also accept "DDMMMYYYY" ("01JUL2010") and ISO style for robustness.
_DATE_FORMATS = (
    "%d-%b-%y",     # 1-Jul-10 / 01-Jul-10
    "%d-%b-%Y",     # 1-Jul-2010
    "%d%b%Y",       # 01JUL2010 / 1JUL2010
    "%d%b%y",       # 01JUL10
    "%Y-%m-%d",     # 2010-07-01
    "%d/%m/%Y",     # 01/07/2010
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AusgridParseError(Exception):
    """Base error raised by the parser."""


class AusgridHeaderError(AusgridParseError):
    """Raised when the CSV header is not usable as expected."""


class AusgridValidationError(AusgridParseError):
    """Raised when data rows contain malformed or unexpected values."""

    def __init__(self, errors: Sequence[str], limit: int = 25) -> None:
        shown = list(errors[:limit])
        hidden = len(errors) - len(shown)
        lines = list(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more error(s)")
        msg = "\n".join(lines)
        super().__init__(msg)
        self.count = len(errors)


# ---------------------------------------------------------------------------
# Structured record
# ---------------------------------------------------------------------------

@dataclass
class IntervalEnergy:
    """A single half-hour energy reading.

    label is the CSV interval label (e.g. "0:30") and kwh is the ENERGY in
    kilowatt-hours measured during that half-hour interval.  No kW conversion
    is performed here -- that belongs to the later profile-processing stage.
    """

    label: str
    kwh: float


@dataclass
class AusgridRecord:
    """One validated row of the wide-format Ausgrid CSV.

    Fields:
        customer_id:            integer customer identifier.
        postcode:               integer postcode.
        generator_capacity_kwp: rated solar capacity in kWp (preserved as-is).
        category:               "GC", "CL" or "GG".
        date:                   Python datetime.date for the row.
        row_quality:            "actual" (blank), "NA", or None when the
                                source CSV has no Row Quality column.
        intervals:              ordered dict {interval_label: kwh}, in the
                                same order the columns appear in the CSV.
        line_number:            physical line number in the source CSV
                                (1-based, including preamble/header rows).
    """

    customer_id: int
    postcode: int
    generator_capacity_kwp: float
    category: str
    date: _dt.date
    row_quality: Optional[str]
    intervals: "OrderedDict[str, float]" = field(default_factory=OrderedDict)
    line_number: Optional[int] = None

    def interval_items(self) -> Iterator[Tuple[str, float]]:
        """Yield (interval_label, kwh) pairs in source column order."""
        return iter(self.intervals.items())

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict view of the record (handy for debugging/export).

        row_quality is serialised as None when the source CSV lacked a
        Row Quality column (i.e. the information is unavailable), and as
        "actual"/"NA" when the column was actually present.
        """
        return {
            "customer_id": self.customer_id,
            "postcode": self.postcode,
            "generator_capacity_kwp": self.generator_capacity_kwp,
            "category": self.category,
            "date": self.date.isoformat(),
            "row_quality": self.row_quality,
            "intervals": dict(self.intervals),
            "line_number": self.line_number,
        }


# ---------------------------------------------------------------------------
# Date + value helpers
# ---------------------------------------------------------------------------

def parse_date(value: str, line: Optional[int] = None) -> _dt.date:
    """Parse an Ausgrid date string into a datetime.date.

    Accepts "1-Jul-10", "01JUL2010", "2010-07-01", etc.
    Raises AusgridValidationError if the value cannot be parsed.
    """
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    where = f" at line {line}" if line else ""
    raise AusgridValidationError([f"invalid date value {value!r}{where}"])


def parse_positive_int(value: str, field_name: str, line: Optional[int] = None) -> int:
    """Parse a strictly positive integer-like cell value."""
    value = value.strip()
    if not re.fullmatch(r"([1-9]\d*)", value):
        where = f" at line {line}" if line else ""
        raise AusgridValidationError(
            [f"invalid {field_name} value {value!r}{where}"]
        )
    return int(value)


def parse_non_negative_float(value: str, field_name: str, line: Optional[int] = None) -> float:
    """Parse a non-negative float-like cell value."""
    value = value.strip()
    try:
        parsed = float(value)
    except ValueError:
        where = f" at line {line}" if line else ""
        raise AusgridValidationError(
            [f"invalid {field_name} value {value!r}{where}"]
        )
    if parsed < 0:
        where = f" at line {line}" if line else ""
        raise AusgridValidationError(
            [f"negative {field_name} value {value!r}{where}"]
        )
    return parsed


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class AusgridParser:
    """Parse the Ausgrid wide-format CSV into validated records.

    Usage:
        parser = AusgridParser("Solar home 2010-2011.csv")
        records = parser.parse()
    """

    def __init__(self, csv_path: str | Path) -> None:
        self.csv_path = Path(csv_path)
        # Populated by parse():
        self.interval_labels: List[str] = []
        self.row_quality_column_present: bool = False
        self.preamble_rows_skipped: int = 0
        self.header_row_number: Optional[int] = None

    # -- low-level access --------------------------------------------------

    def _read_rows(self) -> List[List[str]]:
        """Read all rows as lists of cells using the stdlib csv module.

        The source file uses CRLF line endings; passing newline="" to open
        lets csv handle them without losing or doubling them.
        """
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as fh:
                return list(csv.reader(fh))
        except FileNotFoundError:
            raise AusgridParseError(f"file not found: {self.csv_path}")
        except PermissionError:
            raise AusgridParseError(f"permission denied reading: {self.csv_path}")
        except csv.Error as exc:
            raise AusgridParseError(f"CSV read error in {self.csv_path}: {exc}")

    def _locate_header(self, rows: Sequence[Sequence[str]]) -> Tuple[int, List[str]]:
        """Find the header row and the preamble rows skipped.

        The 2010-2011 file starts with a one-line preamble (a note about
        reading the attached PDF) before the real header.  We scan for the
        first row whose first cell is the expected leading header name.
        """
        for idx, row in enumerate(rows):
            if not row:
                continue
            first = (row[0] or "").strip().lower()
            if first == "customer" or first == "customer id":
                return idx, list(row)
        raise AusgridHeaderError(
            "unable to locate a header row starting with "
            "'Customer' in the CSV; is this an Ausgrid 'Solar home' file?"
        )

    @staticmethod
    def _normalise(name: str) -> str:
        return " ".join((name or "").strip().lower().split())

    def _analyse_header(
        self, header: Sequence[str]
    ) -> Tuple[Dict[str, int], List[str], Optional[int], Dict[str, int]]:
        """Map header names to column indices.

        Returns:
            index_by_name:   normalised header name -> column index for the
                             required metadata fields.
            interval_labels: interval column labels in header order.
            row_quality_index: column index of "Row Quality", or None.
            interval_index:  interval label -> column index.
        Unknown columns are ignored (they may carry opposite-meter data).
        """
        index_by_name: Dict[str, int] = {}
        interval_labels: List[str] = []
        interval_index: Dict[str, int] = {}
        row_quality_index: Optional[int] = None

        for idx, raw in enumerate(header):
            name = self._normalise(raw)
            if not name:
                continue
            if name in REQUIRED_HEADER_FIELDS and name not in index_by_name:
                index_by_name[name] = idx
            elif name in ROW_QUALITY_HEADER_NAMES:
                row_quality_index = idx
            elif _INTERVAL_COLUMN_RE.fullmatch(raw.strip()) and raw.strip() not in interval_index:
                interval_index[raw.strip()] = idx
                interval_labels.append(raw.strip())
            # Unknown columns: ignore.
        return index_by_name, interval_labels, row_quality_index, interval_index

    def _validate_header(
        self,
        index_by_name: Dict[str, int],
        interval_labels: Sequence[str],
    ) -> None:
        missing = sorted(REQUIRED_HEADER_FIELDS - set(index_by_name))
        if missing:
            raise AusgridHeaderError(
                f"required header columns missing from CSV: {missing}"
            )
        if len(interval_labels) != _EXPECTED_INTERVAL_COUNT:
            raise AusgridHeaderError(
                f"expected {_EXPECTED_INTERVAL_COUNT} interval columns "
                f"but found {len(interval_labels)}"
            )
        dups = [lab for lab, n in Counter(interval_labels).items() if n > 1]
        if dups:
            raise AusgridHeaderError(f"duplicate interval columns: {dups}")

    # -- parsing -----------------------------------------------------------

    def parse(self) -> List[AusgridRecord]:
        """Parse the CSV and validate every row.

        Returns a list of AusgridRecord.  Any malformed or unexpected row is
        collected and raised as an AusgridValidationError (nothing is silently
        dropped).
        """
        rows = self._read_rows()
        header_row_idx, header = self._locate_header(rows)
        self.preamble_rows_skipped = header_row_idx
        self.header_row_number = header_row_idx + 1  # 1-based

        index_by_name, interval_labels, row_quality_index, interval_index = self._analyse_header(header)
        self._validate_header(index_by_name, interval_labels)
        self.interval_labels = list(interval_labels)
        self.row_quality_column_present = row_quality_index is not None

        errors: List[str] = []
        records: List[AusgridRecord] = []

        customer_idx = index_by_name["customer"]
        capacity_idx = index_by_name["generator capacity"]
        postcode_idx = index_by_name["postcode"]
        cat_idx = index_by_name["consumption category"]
        date_idx = index_by_name["date"]

        for row_no, row in enumerate(rows[header_row_idx + 1 :]):
            line = header_row_idx + 2 + row_no  # 1-based physical line
            if not row or all((c or "").strip() == "" for c in row):
                # Entirely empty line -- skip silently (trailing newline).
                continue

            if len(row) < max(customer_idx, capacity_idx, postcode_idx, cat_idx, date_idx) + 1:
                errors.append(
                    f"line {line}: row has {len(row)} columns, expected at "
                    f"least {max(customer_idx, capacity_idx, postcode_idx, cat_idx, date_idx) + 1}"
                )
                continue

            # Row Quality: use the column when it actually exists.  When the
            # column is present, blank means "actual" and "NA" means
            # estimated/substitute (per the Ausgrid notes).  When the column
            # is absent, the quality is unknown -- we do NOT infer "actual".
            if row_quality_index is not None:
                raw_q = (row[row_quality_index] if row_quality_index < len(row) else "").strip()
                if raw_q not in ("", ROW_QUALITY_NA):
                    errors.append(
                        f"line {line}: unexpected Row Quality value {raw_q!r}"
                        " (expected blank or 'NA')"
                    )
                    continue
                row_quality = ROW_QUALITY_ACTUAL if raw_q == "" else ROW_QUALITY_NA
            else:
                row_quality = ROW_QUALITY_UNAVAILABLE

            try:
                customer_id = parse_positive_int(row[customer_idx], "Customer", line)
                postcode = parse_positive_int(row[postcode_idx], "Postcode", line)
                capacity = parse_non_negative_float(
                    row[capacity_idx], "Generator Capacity", line
                )
            except AusgridValidationError as exc:
                errors.append(str(exc))
                continue

            category_raw = (row[cat_idx] or "").strip()
            if category_raw not in VALID_CATEGORIES:
                errors.append(
                    f"line {line}: unexpected Consumption Category {category_raw!r}"
                    f" (expected one of {sorted(VALID_CATEGORIES)})"
                )
                continue

            try:
                date = parse_date(row[date_idx], line)
            except AusgridValidationError as exc:
                errors.append(str(exc))
                continue

            intervals: "OrderedDict[str, float]" = OrderedDict()
            row_bad = False
            for label in interval_labels:
                col = interval_index[label]
                raw = (row[col] if col < len(row) else "").strip()
                if raw == "":
                    # Missing interval value --> reported, never silently kept.
                    errors.append(
                        f"line {line}: missing value for interval {label!r}"
                    )
                    row_bad = True
                    continue
                try:
                    intervals[label] = parse_non_negative_float(raw, f"interval {label}", line)
                except AusgridValidationError as exc:
                    errors.append(str(exc))
                    row_bad = True
            if row_bad:
                continue

            records.append(
                AusgridRecord(
                    customer_id=customer_id,
                    postcode=postcode,
                    generator_capacity_kwp=capacity,
                    category=category_raw,
                    date=date,
                    row_quality=row_quality,
                    intervals=intervals,
                    line_number=line,
                )
            )

        if errors:
            raise AusgridValidationError(errors)
        return records

    # -- summary/statistics -------------------------------------------------

    def summarize(self, records: Sequence[AusgridRecord]) -> Dict[str, object]:
        """Compute a validation summary over the parsed records.

        All numbers are computed from the actually parsed data -- nothing is
        hard-coded or assumed.  Row Quality is reported as "unavailable"
        (rather than a bogus per-row value) when the CSV had no Row Quality
        column.
        """
        total = len(records)
        customers = sorted({r.customer_id for r in records})
        dates = {r.date for r in records}
        categories = Counter(r.category for r in records)
        qualities = Counter(r.row_quality for r in records)
        capacities = sorted({round(r.generator_capacity_kwp, 6) for r in records})
        postcodes = sorted({r.postcode for r in records})

        # Per-category customer counts (CL only exists for some customers).
        cust_by_cat = {}
        for cat in VALID_CATEGORIES:
            cust_by_cat[cat] = sorted({r.customer_id for r in records if r.category == cat})

        if self.row_quality_column_present:
            row_quality_report = dict(qualities)
        else:
            row_quality_report = "unavailable"

        return {
            "total_records": total,
            "unique_customers": len(customers),
            "customer_id_min": min(customers) if customers else None,
            "customer_id_max": max(customers) if customers else None,
            "categories": dict(categories),
            "customers_per_category": {c: len(v) for c, v in cust_by_cat.items()},
            "date_min": min(dates).isoformat() if dates else None,
            "date_max": max(dates).isoformat() if dates else None,
            "unique_dates": len(dates),
            "interval_columns": len(records[0].intervals) if records else 0,
            "row_quality_values": row_quality_report,
            "generator_capacity_kwp_min": min(capacities) if capacities else None,
            "generator_capacity_kwp_max": max(capacities) if capacities else None,
            "generator_capacity_distinct_values": len(capacities),
            "postcode_count": len(postcodes),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(summary: Dict[str, object]) -> None:
    print(f"Total parsed records                 : {summary['total_records']}")
    print(f"Unique customers                    : {summary['unique_customers']}"
          f"  (ids {summary['customer_id_min']}..{summary['customer_id_max']})")
    print(f"Date range                          : {summary['date_min']} -> {summary['date_max']}"
          f"  ({summary['unique_dates']} days)")
    print(f"Interval columns (half-hour kWh)    : {summary['interval_columns']}")
    print(f"Consumption categories              : {summary['categories']}")
    print(f"  customers per category            : {summary['customers_per_category']}")
    print(f"Row Quality values                  : {summary['row_quality_values']}")
    print(f"Generator capacity kWp range        : {summary['generator_capacity_kwp_min']}..{summary['generator_capacity_kwp_max']}"
          f"  ({summary['generator_capacity_distinct_values']} distinct)")
    print(f"Distinct postcodes                  : {summary['postcode_count']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ausgrid_parser.py",
        description="Parse and validate an Ausgrid 'Solar home' CSV",
    )
    ap.add_argument("csv_path", help="path to the Ausgrid CSV (e.g. 'Solar home 2010-2011.csv')")
    ap.add_argument(
        "--sample", type=int, default=0, metavar="N",
        help="print the first N parsed records after validation",
    )
    args = ap.parse_args(argv)

    parser = AusgridParser(args.csv_path)
    try:
        records = parser.parse()
    except AusgridParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary = parser.summarize(records)
    print(f"File                                  : {parser.csv_path}")
    print(f"Preamble rows skipped                 : {parser.preamble_rows_skipped}")
    print(f"Row Quality column present            : {parser.row_quality_column_present}")
    _print_summary(summary)

    for i, rec in enumerate(records[: args.sample]):
        print(f"\n--- record {i + 1} (source line {rec.line_number}) ---")
        for k, v in rec.to_dict().items():
            if k == "intervals":
                print("  intervals:")
                for label, kwh in rec.intervals.items():
                    print(f"    {label:<6} {kwh:.6f} kWh")
            else:
                print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())