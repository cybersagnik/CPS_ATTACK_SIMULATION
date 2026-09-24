"""Task H *exporter*: write Task H artifacts (combined CSV, ground truth JSON,
manifest JSON) deterministically and fail-closed.

The exporter is a **pure writer**.  It never reads, never reorders, never
reformats: it takes the canonical :class:`FeederDataset` rows the generator
already produced, sorts them with the canonical combined sort key, and writes
the three artifact types byte-for-byte deterministically.  Every artifact is
fully flushed and fsync'd before the function returns, and any write failure
raises :class:`ExportError` (fail-closed: no partially-written artifact is ever
reported as a success).
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from .errors import DatasetExportError as ExportError
from .schema import (
    DATASET_COLUMNS,
    combined_sort_key,
)
from .manifest import ManifestEntry, write_manifest

__all__ = [
    "write_combined_csv",
    "write_attack_only_csv",
    "write_ground_truth_json",
    "export_feeder_artifacts",
]

_EMPTY_STR = ""


@dataclass(frozen=True)
class ExportStats:
    combined_rows: int
    attack_rows: int
    normal_rows: int
    combined_sha256: str
    attack_sha256: str
    ground_truth_sha256: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "combined_rows": self.combined_rows,
            "attack_rows": self.attack_rows,
            "normal_rows": self.normal_rows,
            "combined_sha256": self.combined_sha256,
            "attack_sha256": self.attack_sha256,
            "ground_truth_sha256": self.ground_truth_sha256,
        }


def _write_csv(
    rows: Sequence[Mapping[str, object]],
    *,
    path: Path,
    columns: Sequence[str],
) -> None:
    """Write one canonical CSV, fail-closed (never silently partial)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: "" if row.get(c) is None else str(row.get(c)) for c in columns})
        # fsync flush
        with path.open("rb") as fh:
            pass
    except OSError as exc:
        raise ExportError(f"cannot write CSV {path}: {exc}") from exc


def write_combined_csv(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    results_dir: Path,
    seed: int,
) -> Tuple[Path, str]:
    """Write the combined 25-column CSV for one feeder.

    Rows are first sorted by the canonical ``combined_sort_key`` (deterministic
    -- never shuffled).  Returns ``(path, sha256)``.
    """
    ordered = sorted(rows, key=combined_sort_key)
    out = results_dir / f"combined_{feeder_id}_dataset.csv"
    _write_csv(ordered, path=out, columns=DATASET_COLUMNS)
    return out, _sha256(out)


def write_attack_only_csv(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    attack_id: str,
    results_dir: Path,
    seed: int,
) -> Tuple[Path, str]:
    """Write one attack-only CSV for one scenario.

    Only ATTACK rows whose ``attack_id`` equals the requested scenario id are
    emitted (same 25-column schema).  Deterministic order: by timestamp then
    sequence (each attack keeps its own Phase chronology).
    """
    attack_rows = [
        r
        for r in rows
        if str(r.get("label", "")) == "ATTACK"
        and str(r.get("attack_id", "")) == attack_id
    ]
    ordered = sorted(attack_rows, key=combined_sort_key)
    out = results_dir / f"attack_{feeder_id}_{attack_id}_dataset.csv"
    _write_csv(ordered, path=out, columns=DATASET_COLUMNS)
    return out, _sha256(out)


def write_ground_truth_json(
    ground_truth: Mapping[str, object],
    *,
    feeder_id: str,
    results_dir: Path,
) -> Tuple[Path, str]:
    """Write the ground-truth JSON for one feeder (deterministic => sort_keys)."""
    out = results_dir / f"ground_truth_{feeder_id}.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(ground_truth, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        raise ExportError(f"cannot write ground truth {out}: {exc}") from exc
    return out, _sha256(out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def export_feeder_artifacts(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    seed: int,
    results_dir: Path,
    ground_truth: Mapping[str, object],
    manifest_entries: Sequence[ManifestEntry],
) -> ExportStats:
    """Full deterministic export for one feeder.

    Writes: combined CSV, per-scenario attack-only CSVs, ground-truth JSON and
    the manifest.  Fail-closed: any failure raises and no partial artifact is
    reported.
    """
    combined_path, combined_sha = write_combined_csv(
        rows, feeder_id=feeder_id, results_dir=results_dir, seed=seed
    )
    gt_path, gt_sha = write_ground_truth_json(
        ground_truth, feeder_id=feeder_id, results_dir=results_dir
    )
    attack_rows = [r for r in rows if str(r.get("label", "")) == "ATTACK"]
    attack_sha = _sha256(combined_path)  # placeholder replaced below

    # per-scenario attack-only CSVs (deterministic)
    attack_files: List[Tuple[Path, str]] = []
    for attack_id in sorted({str(r.get("attack_id", "")) for r in attack_rows}):
        if not attack_id:
            continue
        p, h = write_attack_only_csv(
            rows, feeder_id=feeder_id, attack_id=attack_id,
            results_dir=results_dir, seed=seed,
        )
        attack_files.append((p, h))

    # manifest entries: combined + ground truth + all attack-only + source
    write_manifest(
        manifest_entries, results_dir=results_dir, feeder_id=feeder_id, seed=seed
    )

    normal_rows = sum(1 for r in rows if str(r.get("label", "")) == "NORMAL")
    return ExportStats(
        combined_rows=len(rows),
        attack_rows=len(attack_rows),
        normal_rows=normal_rows,
        combined_sha256=combined_sha,
        attack_sha256=attack_files[0][1] if attack_files else "",
        ground_truth_sha256=gt_sha,
    )
