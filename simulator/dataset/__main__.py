"""Task H command line entry point: deterministic combined dataset generation.

Usage
-----
``python3 -m simulator.dataset --feeders ieee37,ieee123 --seed 42 \
--events-dir results --results-dir results/dataset``

Deterministic, fail-closed: exit code 0 only when every requested feeder is
generated, combined, validated and exported successfully.  No fabrication, no
silent failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from .errors import DatasetError
from simulator.attack.engine import REGISTERED_SCENARIOS
from simulator.attack.scenarios import scenario_id_for

from .exporter import (
    write_attack_only_csv,
    write_combined_csv,
    write_ground_truth_json,
)
from .generator import FeederDataset, generate_feeder_dataset
from .manifest import ManifestEntry, write_manifest
from .schema import validate_combined

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
    write_manifest(
        entries,
        results_dir=results_dir,
        feeder_id=feeder_id,
        seed=seed,
        record={
            "combined_sha256": combined_sha,
            "determinism_digest": combined_sha,
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    feeders = [f.strip() for f in args.feeders.split(",") if f.strip()]
    if not feeders:
        print("error: no feeders provided", file=sys.stderr)
        return 2

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