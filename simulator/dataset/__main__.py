"""Task H command line entry point: deterministic combined dataset generation
and ground-truth validation.

Usage
-----
Generate:

``python3 -m simulator.dataset --feeders ieee37,ieee123 --seed 42 \
--events-dir results --results-dir results/dataset``

Validate ground truth (Task I)::

``python3 -m simulator.dataset --feeders ieee37,ieee123 --seed 42 \
--events-dir results --results-dir results/dataset --validate-ground-truth``

Deterministic, fail-closed: exit code 0 only when every requested feeder is
generated, combined, validated and exported successfully (or validates as
PASS in ``--validate-ground-truth`` mode).  No fabrication, no silent failure,
no silent repair of invalid ground truth.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from .errors import DatasetError
from simulator.attack.engine import REGISTERED_SCENARIOS
from simulator.attack.scenarios import scenario_id_for

from .exporter import (
    write_attack_only_csv,
    write_combined_csv,
    write_ground_truth_json,
)
from .generator import FeederDataset, generate_feeder_dataset
from .ground_truth import (
    GroundTruthValidationReport,
    validate_feeder_ground_truth,
    write_validation_report,
)
from .manifest import ManifestEntry, write_manifest
from .schema import MITRE_TECHNIQUE_COLUMN, validate_combined

__all__ = ["main"]


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="simulator.dataset",
        description=(
            "Task H: deterministic, combined, attached-label CPS attack "
            "dataset generation (fail-closed)."
        ),
    )
    parser.add_argument(
        "--feeders",
        required=True,
        help="comma-separated feeder ids to generate, e.g. ieee37,ieee123",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="deterministic RNG seed (default: 42)",
    )
    parser.add_argument(
        "--events-dir",
        type=Path,
        default=Path("results"),
        help="directory holding normal_<feeder>_network_events.csv (default: results)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("results/dataset"),
        help="output directory (default: results/dataset)",
    )
    parser.add_argument(
        "--validate-ground-truth",
        action="store_true",
        help=(
            "Task I: validate the ground-truth artifacts already in "
            "--results-dir (regenerating any missing artifact first), write "
            "<results-dir>/ground_truth/validation.json and exit 0 only on "
            "PASS (fail-closed, never repairs)"
        ),
    )
    return parser.parse_args(argv)


def _build_ground_truth(
    dataset: FeederDataset,
    *,
    seed: int,
    combined_rows: int,
) -> Mapping[str, object]:
    return {
        "feeder_id": dataset.feeder_id,
        "seed": seed,
        "scenario_id": dataset.scenario_id,
        # verbatim records from the F/G engine / Phase E (never re-fabricated):
        "normal": dict(dataset.ground_truth_normal),
        "attacks": [dict(gt) for gt in dataset.ground_truth_attacks],
        "failed_scenarios": dict(dataset.failed_scenarios),
        "combined_rows": combined_rows,
        "normal_rows": len(dataset.normal_events),
        "attack_rows": len(dataset.attack_events),
    }


def _one_feeder(
    feeder_id: str,
    *,
    seed: int,
    events_dir: Path,
    results_dir: Path,
) -> Mapping[str, object]:
    """Generate + combine + validate + export exactly one feeder (fail-closed)."""
    dataset = generate_feeder_dataset(
        feeder_id,
        seed=seed,
        results_dir=events_dir,
    )
    rows = dataset.combined_rows()
    validate_combined(
        rows,
        feeder_id=feeder_id,
        expected_scenario_ids=[
            scenario_id_for(aid, feeder_id) for aid in sorted(REGISTERED_SCENARIOS)
        ],
        expected_attack_ids=sorted(REGISTERED_SCENARIOS),
    )

    ground_truth = _build_ground_truth(
        dataset, seed=seed, combined_rows=len(rows)
    )

    combined_path, combined_sha = write_combined_csv(
        rows, feeder_id=feeder_id, results_dir=results_dir, seed=seed
    )
    gt_path, gt_sha = write_ground_truth_json(
        ground_truth, feeder_id=feeder_id, results_dir=results_dir
    )

    entries: List[ManifestEntry] = []
    attacks = tuple(ground_truth.get("attacks", ()))
    for attack in attacks:
        attack_id = str(attack.get("attack_id", ""))
        if not attack_id:
            continue
        apath, asha = write_attack_only_csv(
            rows,
            feeder_id=feeder_id,
            attack_id=attack_id,
            results_dir=results_dir,
            seed=seed,
        )
        entries.append(ManifestEntry.from_path(apath, kind="attack"))

    entries.append(ManifestEntry.from_path(combined_path, kind="combined"))
    entries.append(ManifestEntry.from_path(gt_path, kind="ground_truth"))

    scenarios: List[Mapping[str, object]] = [
        {
            "attack_id": str(rec.get("attack_id", "")),
            "feeder_id": feeder_id,
            "status": "success" if rec.get("is_attack") else "failed",
            "event_count": int(rec.get("event_count", 0)),
            "mitre_technique": str(rec.get(MITRE_TECHNIQUE_COLUMN, "")),
            "output": f"attack_{feeder_id}_{rec.get('attack_id', '')}_dataset.csv",
        }
        for rec in attacks
    ]
    scenarios.extend(
        {
            "attack_id": aid,
            "feeder_id": feeder_id,
            "status": "failed",
            "event_count": 0,
            "mitre_technique": "",
            "output": "",
            "failure_reason": reason,
        }
        for aid, reason in sorted(ground_truth.get("failed_scenarios", {}).items())
    )
    scenarios.sort(key=lambda s: str(s.get("attack_id", "")))

    write_manifest(
        entries,
        results_dir=results_dir,
        feeder_id=feeder_id,
        seed=seed,
        record={
            "combined_sha256": combined_sha,
            "determinism_digest": combined_sha,
            "scenarios": scenarios,
        },
    )

    counts = dataset.counts()
    return {
        "feeder_id": feeder_id,
        "combined_rows": counts["combined_rows"],
        "normal_rows": counts["normal_events"],
        "attack_rows": counts["attack_events"],
        "successful_scenarios": counts["successful_scenarios"],
        "failed_scenarios": counts["failed_scenarios"],
        "combined_sha256": combined_sha,
    }


def _read_combined_csv(
    path: Path,
    *,
    feeder_id: str,
) -> List[Mapping[str, object]]:
    """Read one combined CSV back, fail-closed (re-validates the schema)."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows: List[Mapping[str, object]] = [dict(r) for r in csv.DictReader(fh)]
    except OSError as exc:
        raise DatasetError(f"cannot read combined CSV {path}: {exc}") from exc
    if not rows:
        raise DatasetError(f"combined CSV is empty: {path}")
    validate_combined(
        rows,
        feeder_id=feeder_id,
        expected_scenario_ids=[
            scenario_id_for(aid, feeder_id) for aid in sorted(REGISTERED_SCENARIOS)
        ],
        expected_attack_ids=sorted(REGISTERED_SCENARIOS),
    )
    return rows


def _read_json(path: Path) -> Mapping[str, object]:
    """Read one JSON artifact back, fail-closed."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DatasetError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DatasetError(f"JSON artifact is not an object: {path}")
    return data


def _validate_one_feeder(
    feeder_id: str,
    *,
    seed: int,
    events_dir: Path,
    results_dir: Path,
) -> Tuple[GroundTruthValidationReport, bool]:
    """Validate one feeder's ground-truth artifacts (regenerating when absent).

    Returns ``(report, regenerated)``.  Missing combined/ground-truth artifacts
    are first produced by the existing ``_one_feeder`` pipeline (never
    fabricated), then the on-disk artifacts are read back verbatim, re-validated
    against the canonical schemas and checked against the ground truth.
    """
    combined_path = results_dir / f"combined_{feeder_id}_dataset.csv"
    gt_path = results_dir / f"ground_truth_{feeder_id}.json"
    manifest_path = results_dir / f"manifest_{feeder_id}.json"
    regenerated = not (combined_path.exists() and gt_path.exists())
    if regenerated:
        _one_feeder(
            feeder_id, seed=seed, events_dir=events_dir, results_dir=results_dir
        )
    report = validate_feeder_ground_truth(
        feeder_id=feeder_id,
        combined_rows=_read_combined_csv(combined_path, feeder_id=feeder_id),
        ground_truth=_read_json(gt_path),
        manifest=_read_json(manifest_path) if manifest_path.exists() else None,
    )
    return report, regenerated


def _run_validation(
    feeders: Sequence[str],
    *,
    seed: int,
    events_dir: Path,
    results_dir: Path,
) -> int:
    """``--validate-ground-truth`` mode (Task I, fail-closed, never repairs)."""
    failures: List[str] = []
    reports: List[GroundTruthValidationReport] = []
    for feeder_id in feeders:
        try:
            report, regenerated = _validate_one_feeder(
                feeder_id, seed=seed, events_dir=events_dir, results_dir=results_dir
            )
            reports.append(report)
            print(
                f"{report.validation_status:4s} {feeder_id}: "
                f"total={report.total_events} normal={report.normal_events} "
                f"attack={report.attack_events}"
            )
            for scenario in report.scenarios:
                print(
                    f"    {scenario.attack_id}: events={scenario.event_count} "
                    f"window={scenario.window_start}..{scenario.window_end} "
                    f"mitre={scenario.mitre_technique_ids} "
                    f"status={scenario.status}"
                )
            if regenerated:
                print(f"    (missing artifacts regenerated on-demand, seed={seed})")
        except Exception as exc:  # noqa: BLE001 -- fail-closed
            failures.append(f"{feeder_id}: {exc}")
            print(f"FAIL {feeder_id}: {exc}", file=sys.stderr)

    if failures:
        print(
            f"Ground-truth validation: {len(failures)} feeder(s) FAILED "
            "(fail-closed)",
            file=sys.stderr,
        )
        return 1

    out, sha = write_validation_report(reports, results_dir=results_dir)
    status = "PASS" if all(r.validation_status == "PASS" for r in reports) else "FAIL"
    print(f"report: {out}")
    print(f"sha256: {sha}")
    print(f"Ground-truth validation: {status}")
    return 0 if status == "PASS" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    feeders = [f.strip() for f in args.feeders.split(",") if f.strip()]
    if not feeders:
        print("error: no feeders provided", file=sys.stderr)
        return 2

    if args.validate_ground_truth:
        return _run_validation(
            feeders,
            seed=args.seed,
            events_dir=args.events_dir,
            results_dir=args.results_dir,
        )

    failures: List[str] = []
    results: List[Mapping[str, object]] = []
    for feeder_id in feeders:
        try:
            results.append(_one_feeder(
                feeder_id,
                seed=args.seed,
                events_dir=args.events_dir,
                results_dir=args.results_dir,
            ))
            print(f"OK   {feeder_id}")
        except Exception as exc:  # noqa: BLE001 -- fail-closed: report everything
            failures.append(f"{feeder_id}: {exc}")
            print(f"FAIL {feeder_id}: {exc}", file=sys.stderr)

    for r in results:
        print(
            f"  {r['feeder_id']}: combined={r['combined_rows']} "
            f"normal={r['normal_rows']} attack={r['attack_rows']} "
            f"scenarios_ok={r['successful_scenarios']} "
            f"scenarios_failed={r['failed_scenarios']}"
            f" sha256={r['combined_sha256']}"
        )

    if failures:
        print(f"Task H: {len(failures)} feeder(s) FAILED (fail-closed)", file=sys.stderr)
        return 1
    print(f"Task H: {len(results)} feeder(s) OK, deterministic (seed={args.seed}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())