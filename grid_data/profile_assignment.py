#!/usr/bin/env python3
"""Uniform random assignment + CSV export (Phase B-7 / B-8).

Stage in the CPS project (Phase B / Steps 7 and 8):
    ausgrid_profiles_2010_2011.csv
        |
        v
    profile_engine.py                     <- Phase B-6 (ProfileEngine)
        |
        v
    profile_assignment.py                 <- Phase B-7 + B-8 (THIS module)
        |
        v
    list[ProfileAssignment]  (assignment_id -> customer_id)
        |
        v
    profile_assignment.csv                <- Phase B-8 (exporter below)
        |
        v
    Phase C (feeder binding, IEEE feeders) <- future (teammate's feeder model)

B-7 is COMPLETELY FEEDER-AGNOSTIC.  It has no knowledge of:
    IEEE-37, IEEE-123, feeder/load/generator/bus ids, phases, kw_per_phase,
    Common Grid Model, get_load_targets()/get_generator_targets(), SCADA,
    simulation, attack logic, MITRE ATT&CK.

One assignment unit is:

    assignment_id -> customer_id

(e.g. assignment 1 -> customer 17).  B-7 does NOT decide that a slot maps to a
feeder node — that is Phase C's responsibility.

Randomization semantics:
    * Uniform random sampling WITHOUT replacement (default).
    * Configurable integer seed; default seed = 42.
    * Every eligible customer (every customer in the ProfileEngine) has equal
      probability.  No weighting, no postcode/capacity/CL/load/solar matching,
      no hidden constraints:
        "Uniform random assignment across eligible Ausgrid customers."
    * Uses a dedicated deterministic  random.Random(seed) generator — never
      uncontrolled global randomness.
    * Same eligibility pool + same assignment_count + same seed
      => identical ordered assignment.  Different seed normally differs.

Load + solar coupling:
    A customer is a complete household profile.  When customer X is assigned,
    BOTH its load profile (GC + CL) and solar profile (GG) belong to that same
    assignment.  B-7 never samples load from one customer and solar from
    another.

CL handling:
    All customers are equally eligible (customers without CL simply have
    cl_kwh = 0.0 and load = GC + 0).  No CL eligibility rule exists.

cl_present semantics:
    cl_present = True iff the selected customer's profiles contain at least
    one NON-ZERO cl_kwh value, per ProfileEngine.customer_has_cl()
    (value-presence; the CL category count of 139 differs from the
    value-presence count of 136 for the real dataset — see B-6 docs).

Solar capacity:
    generator_capacity_kwp is METADATA only: carried through unchanged, never
    used for eligibility, never clamped/normalised/modified.

The same assignment is re-used for Normal and Attack simulation (no
re-randomization between them).  B-7 only generates the assignment; Phase B-8
persists it to profile_assignment.csv as assignment metadata
(assignment_id -> customer_id) for later orchestration to reuse.

Phase B-8 (this module) is an EXPORT/PERSISTENCE layer only:
    * It writes the validated B-7 ProfileAssignment records to a CSV.
    * Exact columns: assignment_id,customer_id,generator_capacity_kwp,seed,cl_present.
    * No feeder/load/bus/generator ids, no phases, no profile samples:
      the full 17,520-sample customer profile remains accessible only through
      ProfileEngine.get_customer_profile(customer_id).
    * Deterministic: same B-7 assignments -> identical CSV bytes.
    * Atomic write (temp file then os.replace) so a failed export never leaves
      a partial final CSV.

API:
    assigner = RandomProfileAssigner(ProfileEngine("ausgrid_profiles_2010_2011.csv"))
    assignments = assigner.assign(assignment_count=25, seed=42)   # list[ProfileAssignment]
    assigner.validate_assignments(assignments)
    assigner.summary(assignments)

    exporter = ProfileAssignmentExporter()
    exporter.export(assignments, "profile_assignment.csv",
                    assigner=assigner, requested_count=25)
    report = exporter.validate_file("profile_assignment.csv", expected_count=25)

CLI:
    python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 25 --seed 42
    python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 25 --seed 42 --output profile_assignment.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from profile_engine import ProfileEngine, ProfileEngineError

DEFAULT_SEED = 42

# Exact locked schema of the Phase B-8 assignment CSV (column order fixed).
ASSIGNMENT_COLUMNS = [
    "assignment_id",
    "customer_id",
    "generator_capacity_kwp",
    "seed",
    "cl_present",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProfileAssignmentError(Exception):
    """Raised for invalid assignment requests or invalid assignment results."""


# ---------------------------------------------------------------------------
# Assignment record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileAssignment:
    """One customer-level assignment slot: assignment_id -> customer_id.

    Fields:
        assignment_id:            stable sequential slot id (1..N).
        customer_id:              Ausgrid customer selected for this slot.
        generator_capacity_kwp:   rated solar capacity (metadata, unchanged).
        seed:                     seed that produced this assignment.
        cl_present:               True iff customer has any non-zero CL energy.

    Deliberately absent (Phase C adds these later):
        feeder_id, load_id, generator_id, bus_id, phase.
    """

    assignment_id: int
    customer_id: int
    generator_capacity_kwp: float
    seed: int
    cl_present: bool

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict view for B-8 CSV export."""
        return {
            "assignment_id": self.assignment_id,
            "customer_id": self.customer_id,
            "generator_capacity_kwp": self.generator_capacity_kwp,
            "seed": self.seed,
            "cl_present": self.cl_present,
        }


# ---------------------------------------------------------------------------
# Assigner
# ---------------------------------------------------------------------------

class RandomProfileAssigner:
    """Uniform, no-replacement random assignment of customers to generic slots.

    The assigner is bound to a single :class:`ProfileEngine`; it reuses the
    engine's discovery of eligible customers and per-customer metadata.
    """

    def __init__(self, profile_engine: ProfileEngine) -> None:
        self.engine = profile_engine
        self._eligible: Optional[List[int]] = None

    # -- public API ----------------------------------------------------------

    @property
    def eligible_customer_ids(self) -> List[int]:
        """All customers eligible for assignment (whole engine pool)."""
        if self._eligible is None:
            self._eligible = list(self.engine.get_customer_ids())
        return list(self._eligible)

    def assign(
        self,
        assignment_count: int,
        seed: int = DEFAULT_SEED,
    ) -> List[ProfileAssignment]:
        """Draw `assignment_count` customers uniformly WITHOUT replacement.

        Args:
            assignment_count: number of generic assignment slots (0 allowed).
            seed: deterministic seed for the dedicated random generator
                  (integer).  Default 42.

        Returns:
            list[ProfileAssignment] ordered by assignment_id (1..N).

        Raises:
            ProfileAssignmentError: count < 0 or count > number of eligible
                customers (no-replacement mode).
        """
        if not isinstance(assignment_count, int):
            raise ProfileAssignmentError(
                f"assignment_count must be an int, got {type(assignment_count).__name__}"
            )
        if assignment_count < 0:
            raise ProfileAssignmentError(
                f"assignment_count must be >= 0, got {assignment_count}"
            )
        pool = self.eligible_customer_ids
        if assignment_count > len(pool):
            raise ProfileAssignmentError(
                f"assignment_count {assignment_count} exceeds the number of "
                f"eligible customers ({len(pool)}) in no-replacement mode"
            )

        rng = random.Random(seed)
        selected = rng.sample(pool, assignment_count)

        assignments: List[ProfileAssignment] = []
        for slot, customer_id in enumerate(selected, start=1):
            meta = self.engine.get_customer_metadata(customer_id)
            cl_present = self.engine.customer_has_cl(customer_id)
            capacity = meta[1] if meta is not None else None
            if capacity is None:
                raise ProfileAssignmentError(
                    f"assigned customer {customer_id} missing from ProfileEngine"
                )
            assignments.append(
                ProfileAssignment(
                    assignment_id=slot,
                    customer_id=customer_id,
                    generator_capacity_kwp=capacity,
                    seed=seed,
                    cl_present=cl_present,
                )
            )
        return assignments

    def validate_assignments(
        self,
        assignments: Sequence[ProfileAssignment],
        requested_count: Optional[int] = None,
    ) -> Dict[str, object]:
        """Validate an assignment list (generic, not hard-coded to any dataset).

        Checks:
          1. count matches the requested count (when supplied);
          2. assignment ids are unique and sequential 1..N;
          3. customer ids are unique (no-replacement mode);
          4. every assigned customer exists in the ProfileEngine;
          5. generator_capacity_kwp matches the ProfileEngine metadata;
          6. seed is recorded consistently on every record;
          7. cl_present matches the documented (non-zero CL) definition;
          8. every selected customer comes from the eligible pool.
        """
        violations = {k: [] for k in (
            "count_mismatch", "assignment_id_duplicate", "assignment_id_gap",
            "customer_duplicate", "customer_missing", "capacity_mismatch",
            "seed_inconsistent", "cl_mismatch", "customer_outside_pool",
        )}

        if requested_count is not None and len(assignments) != requested_count:
            violations["count_mismatch"].append(
                (requested_count, len(assignments))
            )

        ids_seen: Set[int] = set()
        customers_seen: Set[int] = set()
        pool = set(self.eligible_customer_ids)
        seeds: Set[int] = set()

        for i, a in enumerate(assignments, start=1):
            if a.assignment_id in ids_seen:
                violations["assignment_id_duplicate"].append(a.assignment_id)
            if a.assignment_id != i:
                violations["assignment_id_gap"].append((i, a.assignment_id))
            ids_seen.add(a.assignment_id)

            if a.customer_id in customers_seen:
                violations["customer_duplicate"].append(a.customer_id)
            customers_seen.add(a.customer_id)

            meta = self.engine.get_customer_metadata(a.customer_id)
            if meta is None:
                violations["customer_missing"].append(a.customer_id)
                continue
            if a.generator_capacity_kwp != meta[1]:
                violations["capacity_mismatch"].append(
                    (a.customer_id, a.generator_capacity_kwp, meta[1])
                )

            seeds.add(a.seed)
            expected_cl = self.engine.customer_has_cl(a.customer_id)
            if a.cl_present != expected_cl:
                violations["cl_mismatch"].append(
                    (a.customer_id, a.cl_present, expected_cl)
                )

            if a.customer_id not in pool:
                violations["customer_outside_pool"].append(a.customer_id)

        checks = {
            "count_matches_request": len(violations["count_mismatch"]) == 0,
            "assignment_ids_unique": len(violations["assignment_id_duplicate"]) == 0,
            "assignment_ids_sequential": len(violations["assignment_id_gap"]) == 0,
            "customer_ids_unique": len(violations["customer_duplicate"]) == 0,
            "customers_exist_in_engine": len(violations["customer_missing"]) == 0,
            "capacities_match_engine": len(violations["capacity_mismatch"]) == 0,
            "seed_recorded": len(seeds) <= 1,
            "cl_present_matches_definition": len(violations["cl_mismatch"]) == 0,
            "customers_within_eligible_pool": len(violations["customer_outside_pool"]) == 0,
        }

        return {
            "total_assignments": len(assignments),
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
        }

    def summary(self, assignments: Sequence[ProfileAssignment]) -> Dict[str, object]:
        """Concise summary of an assignment list (no CSV writing)."""
        if not assignments:
            return {
                "assignment_count": 0,
                "seed": None,
                "unique_customers": 0,
                "cl_present_count": 0,
                "capacity_range": None,
            }
        capacities = [a.generator_capacity_kwp for a in assignments]
        seeds = {a.seed for a in assignments}
        return {
            "assignment_count": len(assignments),
            "seed": seeds.pop() if len(seeds) == 1 else sorted(seeds),
            "unique_customers": len({a.customer_id for a in assignments}),
            "cl_present_count": sum(1 for a in assignments if a.cl_present),
            "capacity_range": (min(capacities), max(capacities)),
        }


# ---------------------------------------------------------------------------
# CSV exporter (Phase B-8)
# ---------------------------------------------------------------------------

class ProfileAssignmentExportError(Exception):
    """Raised when invalid assignments block export or a CSV fails validation."""


def _parse_bool_token(token: str) -> bool:
    """Strict boolean parsing: accept only the canonical True/False strings."""
    if token == "True":
        return True
    if token == "False":
        return False
    raise ProfileAssignmentExportError(
        f"invalid cl_present token {token!r}; expected 'True' or 'False'"
    )


class ProfileAssignmentExporter:
    """Persist validated B-7 ProfileAssignment records to profile_assignment.csv.

    Purely an export layer: it never re-randomizes and never touches the
    customer profiles.  The written file contains assignment metadata only
    (no feeder ids, no phases, no sample rows).
    """

    # -- writing ---------------------------------------------------------------

    def export(
        self,
        assignments: Sequence[ProfileAssignment],
        output_path: str | Path,
        assigner: Optional[RandomProfileAssigner] = None,
        requested_count: Optional[int] = None,
    ) -> Path:
        """Validate then atomically write the assignments to a CSV.

        Validation before export (fails loudly, never writes corrupt output):
          1. structural, engine-free checks (this exporter);
          2. if `assigner` is given, RandomProfileAssigner.validate_assignments()
             (semantic checks: customers exist in engine, capacities match,
             cl_present matches, customers within eligible pool).

        The file is written to a temp sibling and atomically replaced only
        after every row has been written successfully.
        """
        structural = self._structural_checks(assignments, requested_count)
        if not structural["all_passed"]:
            raise ProfileAssignmentExportError(
                "refusing to export invalid assignments: "
                f"{structural['violation_examples']}"
            )
        if assigner is not None:
            r = assigner.validate_assignments(
                assignments, requested_count=requested_count
            )
            if not r["all_passed"]:
                raise ProfileAssignmentExportError(
                    "refusing to export assignments failing B-7 validation: "
                    f"{r['violation_examples']}"
                )

        out = Path(output_path)
        parent = out.parent
        # temp file lives next to the target so os.replace is atomic on the
        # same filesystem.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(parent) if str(parent) else ".", prefix=out.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(ASSIGNMENT_COLUMNS)
                for a in assignments:
                    writer.writerow([
                        a.assignment_id,
                        a.customer_id,
                        a.generator_capacity_kwp,
                        a.seed,
                        "True" if a.cl_present else "False",
                    ])
            os.replace(tmp_name, out)
            try:
                # mkstemp creates 0600; relax to rw-r--r-- so Normal/Attack
                # phases (possibly run by other users) can read the artifact.
                os.chmod(out, 0o644)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return out

    @staticmethod
    def _structural_checks(
        assignments: Sequence[ProfileAssignment],
        requested_count: Optional[int],
    ) -> Dict[str, object]:
        """Engine-free sanity checks on the assignment objects (never duplicates
        B-7's semantic validation; it guards the file format itself)."""
        violations: Dict[str, list] = {
            "count_mismatch": [],
            "non_assignment_record": [],
            "bad_type": [],
            "assignment_id_duplicate": [],
            "assignment_id_gap": [],
            "customer_duplicate": [],
            "capacity_missing": [],
            "seed_inconsistent": [],
            "cl_present_non_bool": [],
        }

        if requested_count is not None:
            if len(assignments) != requested_count:
                violations["count_mismatch"].append(
                    (requested_count, len(assignments))
                )

        ids_seen: Set[int] = set()
        customers_seen: Set[int] = set()
        seeds: Set[int] = set()

        for i, a in enumerate(assignments, start=1):
            if not isinstance(a, ProfileAssignment):
                violations["non_assignment_record"].append(type(a).__name__)
                continue
            if not isinstance(a.assignment_id, int):
                violations["bad_type"].append(("assignment_id", a.assignment_id))
            else:
                if a.assignment_id in ids_seen:
                    violations["assignment_id_duplicate"].append(a.assignment_id)
                elif a.assignment_id != i:
                    violations["assignment_id_gap"].append((i, a.assignment_id))
                ids_seen.add(a.assignment_id)

            if not isinstance(a.customer_id, int):
                violations["bad_type"].append(("customer_id", a.customer_id))
            elif a.customer_id in customers_seen:
                violations["customer_duplicate"].append(a.customer_id)
            customers_seen.add(a.customer_id)

            cap = a.generator_capacity_kwp
            if cap is None or not isinstance(cap, (int, float)):
                violations["capacity_missing"].append(a.customer_id)

            if not isinstance(a.seed, int):
                violations["bad_type"].append(("seed", a.seed))
            else:
                seeds.add(a.seed)

            if not isinstance(a.cl_present, bool):
                violations["cl_present_non_bool"].append(
                    (a.assignment_id, a.cl_present)
                )

        checks = {
            "count_matches_request": len(violations["count_mismatch"]) == 0,
            "all_assignment_records": len(violations["non_assignment_record"]) == 0,
            "field_types_valid": len(violations["bad_type"]) == 0,
            "assignment_ids_unique": len(violations["assignment_id_duplicate"]) == 0,
            "assignment_ids_sequential": len(violations["assignment_id_gap"]) == 0,
            "customer_ids_unique": len(violations["customer_duplicate"]) == 0,
            "capacities_present": len(violations["capacity_missing"]) == 0,
            "seed_consistent": len(seeds) <= 1,
            "cl_present_boolean": len(violations["cl_present_non_bool"]) == 0,
        }
        return {
            "total_assignments": len(assignments),
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
        }

    # -- reading back / CSV validation ------------------------------------------

    @staticmethod
    def read_file(output_path: str | Path) -> Tuple[List[str], List[dict]]:
        """Read the assignment CSV as (columns, list of parsed row dicts).

        Raises ProfileAssignmentExportError on any malformed row.
        """
        path = Path(output_path)
        columns: Optional[List[str]] = None
        rows: List[dict] = []
        try:
            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                first = next(reader, None)
                if first is None:
                    raise ProfileAssignmentExportError(
                        f"{path}: empty CSV (no header)"
                    )
                columns = first
                for line_no, fields in enumerate(reader, start=2):
                    if fields == []:
                        raise ProfileAssignmentExportError(
                            f"{path}: blank row on line {line_no}"
                        )
                    if len(fields) != len(ASSIGNMENT_COLUMNS):
                        raise ProfileAssignmentExportError(
                            f"{path}: row {line_no} has {len(fields)} fields, "
                            f"expected {len(ASSIGNMENT_COLUMNS)}"
                        )
                    try:
                        rows.append({
                            "assignment_id": int(fields[0]),
                            "customer_id": int(fields[1]),
                            "generator_capacity_kwp": float(fields[2]),
                            "seed": int(fields[3]),
                            "cl_present": _parse_bool_token(fields[4]),
                        })
                    except ValueError as exc:
                        raise ProfileAssignmentExportError(
                            f"{path}: row {line_no} unparsable: {fields!r}"
                        ) from exc
        except OSError as exc:
            raise ProfileAssignmentExportError(
                f"{path}: cannot read CSV: {exc}"
            ) from exc
        return columns or [], rows

    @classmethod
    def validate_file(
        cls,
        output_path: str | Path,
        expected_count: Optional[int] = None,
    ) -> Dict[str, object]:
        """Read back the written CSV and verify it round-trips the B-7 records.

        Checks:
          exact header (no unexpected columns), no missing fields,
          row count, assignment ids unique + sequential 1..N, customer ids
          unique, capacities parse as floats, seed consistent,
          cl_present boolean.
        """
        violations: Dict[str, list] = {
            "header_mismatch": [],
            "row_count_mismatch": [],
            "no_missing_fields": [],
            "assignment_id_duplicate": [],
            "assignment_id_gap": [],
            "customer_duplicate": [],
            "capacity_invalid": [],
            "seed_inconsistent": [],
            "seed_invalid": [],
            "cl_present_invalid": [],
        }

        try:
            with open(output_path, "r", newline="") as f:
                reader = csv.reader(f)
                columns = next(reader, None)
                if columns is None:
                    raise ProfileAssignmentExportError(
                        f"{output_path}: empty CSV (no header)"
                    )
        except OSError as exc:
            raise ProfileAssignmentExportError(
                f"{output_path}: cannot read CSV: {exc}"
            ) from exc

        if columns != ASSIGNMENT_COLUMNS:
            violations["header_mismatch"].append(columns)

        try:
            _, rows = cls.read_file(output_path)
        except ProfileAssignmentExportError as exc:
            rows = []
            violations["no_missing_fields"] = [str(exc)]

        if expected_count is not None and len(rows) != expected_count:
            violations["row_count_mismatch"].append((len(rows), expected_count))

        ids_seen: Set[int] = set()
        customers_seen: Set[int] = set()
        seeds: Set[int] = set()
        for i, row in enumerate(rows, start=1):
            fid = row["assignment_id"]
            cid = row["customer_id"]
            if fid in ids_seen:
                violations["assignment_id_duplicate"].append(fid)
            if fid != i:
                violations["assignment_id_gap"].append((i, fid))
            ids_seen.add(fid)

            if cid in customers_seen:
                violations["customer_duplicate"].append(cid)
            customers_seen.add(cid)

            try:
                float(row["generator_capacity_kwp"])
            except (TypeError, ValueError):
                violations["capacity_invalid"].append(cid)

            seed = row["seed"]
            if isinstance(seed, int):
                seeds.add(seed)
            else:
                violations["seed_invalid"].append(cid)

            if row["cl_present"] is None:
                violations["cl_present_invalid"].append(fid)

        checks = {
            "header_exact": len(violations["header_mismatch"]) == 0,
            "row_count_matches": len(violations["row_count_mismatch"]) == 0,
            "no_missing_fields": len(violations["no_missing_fields"]) == 0,
            "assignment_ids_unique": len(violations["assignment_id_duplicate"]) == 0,
            "assignment_ids_sequential": len(violations["assignment_id_gap"]) == 0,
            "customer_ids_unique": len(violations["customer_duplicate"]) == 0,
            "capacities_valid": len(violations["capacity_invalid"]) == 0,
            "seed_consistent": len(seeds) <= 1,
            "seed_valid": len(violations["seed_invalid"]) == 0,
            "cl_present_boolean": len(violations["cl_present_invalid"]) == 0,
        }
        return {
            "file": str(Path(output_path)),
            "header": columns,
            "row_count": len(rows),
            "checks": checks,
            "all_passed": all(checks.values()),
            "violation_examples": {k: v[:5] for k, v in violations.items()},
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="profile_assignment.py",
        description=(
            "Uniform random assignment of Ausgrid customers to generic slots "
            "(Phase B-7). Feeder-agnostic: no feeder/bus/load/generator ids."
        ),
    )
    ap.add_argument(
        "profile_csv",
        help="path to the A-4 profiles CSV (e.g. ausgrid_profiles_2010_2011.csv)",
    )
    ap.add_argument(
        "--count", type=int, required=True,
        help="number of generic assignment slots to draw",
    )
    ap.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help=f"deterministic seed (default: {DEFAULT_SEED})",
    )
    ap.add_argument(
        "--validate", action="store_true",
        help="run validate_assignments() and print the report",
    )
    ap.add_argument(
        "--detail", type=int, default=5, metavar="N",
        help="print the first N assignments (default: 5)",
    )
    ap.add_argument(
        "--output", type=str, metavar="PATH", default=None,
        help="export the assignment to this CSV (Phase B-8). Without it, no "
             "file is written (existing B-7 CLI behavior is preserved).",
    )
    args = ap.parse_args(argv)

    try:
        engine = ProfileEngine(args.profile_csv)
        assigner = RandomProfileAssigner(engine)
        assignments = assigner.assign(args.count, seed=args.seed)
    except (ProfileEngineError, ProfileAssignmentError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.output is not None:
        in_path = Path(args.profile_csv).resolve()
        out_path = Path(args.output).resolve()
        if out_path == in_path:
            print(
                f"ERROR: refusing to overwrite the input profile CSV "
                f"{in_path} with assignment output",
                file=sys.stderr,
            )
            return 1

    s = assigner.summary(assignments)
    print(f"ProfileEngine             : {engine.path}")
    print(f"Eligible customers        : {len(assigner.eligible_customer_ids)}")
    print(f"assignment_count          : {s['assignment_count']}")
    print(f"seed                      : {s['seed']}")
    print(f"unique_customers          : {s['unique_customers']}")
    print(f"cl_present_count          : {s['cl_present_count']}")
    if s["capacity_range"]:
        print(f"generator_capacity_range  : {s['capacity_range'][0]}..{s['capacity_range'][1]} kWp")

    print("\nFirst assignments:")
    for a in assignments[: max(args.detail, 0)]:
        print(f"  assignment {a.assignment_id:>4} -> customer {a.customer_id:>3} "
              f"(kWp={a.generator_capacity_kwp}, cl_present={a.cl_present})")

    if args.validate:
        r = assigner.validate_assignments(assignments, requested_count=args.count)
        print("\nvalidate_assignments():")
        for name, ok in r["checks"].items():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {name}")
        print(f"  all checks passed      : {r['all_passed']}")
        for key, examples in r["violation_examples"].items():
            if examples:
                print(f"  violations [{key}]      : {examples}")

    if args.output is not None:
        exporter = ProfileAssignmentExporter()
        written = exporter.export(
            assignments,
            args.output,
            assigner=assigner,
            requested_count=args.count,
        )
        report = exporter.validate_file(args.output, expected_count=args.count)
        print(f"\nExported (Phase B-8)      : {written}")
        print(f"rows + header            : {report['row_count']} rows + 1 header")
        print(f"csv validate_file()      :")
        for name, ok in report["checks"].items():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {name}")
        print(f"  all checks passed      : {report['all_passed']}")
        for key, examples in report["violation_examples"].items():
            if examples:
                print(f"  violations [{key}]      : {examples}")

    return 0


if __name__ == "__main__":
    sys.exit(main())