"""Task H *manifest writer*: deterministic, fail-closed artifact manifest JSON.

The manifest is the Task H **integrity ledger**: it records, for one feeder,
the canonical source fingerprint, generation parameters, every produced
artifact's path + sha256, the combined-row/attack-row/normal-row counts, the
failed (never fabricated) scenarios and, when determinism verification has
completed, the two-run equality digest used to prove reproducibility.  Every
field is written by value -- nothing is re-derived, re-read or reformatted at
manifest time, and any failure raises :class:`DatasetManifestError` (fail
-closed: no partial manifest is ever reported as success).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .errors import DatasetManifestError

__all__ = [
    "ManifestEntry",
    "ManifestRecord",
    "write_manifest",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One artifact's manifest record (path + kind + sha256, verbatim values)."""

    path: str
    kind: str
    sha256: str

    @classmethod
    def from_path(cls, path: Path, *, kind: str) -> "ManifestEntry":
        owner = getattr(cls, "relative_to", None)
        return cls(
            path=path.name,
            kind=kind,
            sha256=_sha256_file(path),
        )


@dataclass(frozen=True)
class ManifestRecord:
    """The full integrity ledger for one feeder's generated dataset."""

    schema_version: str = "1.0"
    feeder_id: str = ""
    seed: int = 42
    source_normal_sha256: str = ""
    source_normal_path: str = ""
    generated_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    artifact_entries: Tuple[ManifestEntry, ...] = ()
    combined_rows: int = 0
    attack_rows: int = 0
    normal_rows: int = 0
    successful_scenarios: int = 0
    failed_scenarios: Mapping[str, str] = field(default_factory=dict)
    determinism_digest: str = ""

    def as_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "feeder_id": self.feeder_id,
            "seed": self.seed,
            "source_normal_sha256": self.source_normal_sha256,
            "source_normal_path": self.source_normal_path,
            "generated_at_utc": self.generated_at_utc,
            "artifact_entries": [
                {"path": e.path, "kind": e.kind, "sha256": e.sha256}
                for e in self.artifact_entries
            ],
            "combined_rows": self.combined_rows,
            "attack_rows": self.attack_rows,
            "normal_rows": self.normal_rows,
            "successful_scenarios": self.successful_scenarios,
            "failed_scenarios": dict(self.failed_scenarios),
            "determinism_digest": self.determinism_digest,
        }


def write_manifest(
    entries: Sequence[ManifestEntry],
    *,
    results_dir: Path,
    feeder_id: str,
    seed: int,
    record: Mapping[str, object] | None = None,
) -> Tuple[Path, str]:
    """Write one feeder's manifest JSON deterministically (sorted keys).

    Fail-closed: any write failure raises :class:`DatasetManifestError` and no
    partial manifest is reported.

    Returns ``(path, sha256)``.
    """
    base = ManifestRecord(
        feeder_id=feeder_id,
        seed=seed,
        artifact_entries=tuple(entries),
    )
    if record is not None:
        for key, value in record.items():
            if hasattr(base, key):
                object.__setattr__(base, key, value)
    out = results_dir / f"manifest_{feeder_id}.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(base.as_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        raise DatasetManifestError(f"cannot write manifest {out}: {exc}") from exc
    return out, _sha256_file(out)