"""Task H *generator*: build one feeder's deterministic combined 25-column dataset.

Task H's generator is a **strict composition** over the Phase F/G engine: it
*consumes* the canonical F/G surface (``AttackContext``, ``AttackEngine``,
``AttackResult.ground_truth()``/``event_rows()``/``as_dict()``, the
``REGISTERED_SCENARIOS`` registry, ``scenario_id_for`` and the ``RESULT_*``
constants) and the canonical Phase E surface (``NETWORK_EVENT_COLUMNS`` plus the
canonical normal network-events CSV).  It produces the **combined** artifact in
the canonical 25-column schema.  It does **not** re-implement, reformat,
renumber, rename, refactor, fabricate or shuffle anything: every row value comes
verbatim from the Phase E CSV or from the Phase F/G engine result.  Fail-closed
throughout: any scenario that fails is recorded as failed (with its real
failure reason), is **never** fabricated, and never contributes partial rows.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from simulator.attack.base import AttackContext
from simulator.attack.engine import AttackEngine
from simulator.attack.events import AttackEvent
from simulator.attack.results import AttackResult, RESULT_SUCCESS
from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for
from simulator.network.events import NETWORK_EVENT_COLUMNS
from simulator.power.loader import FeederLoader

from .errors import (
    DatasetError,
    DatasetGenerationError,
    DatasetValidationError,
)
from .schema import (
    ATTACK_ID_COLUMN,
    DATASET_COLUMNS,
    LABEL_ATTACK,
    LABEL_COLUMN,
    LABEL_NORMAL,
    MITRE_TECHNIQUE_COLUMN,
    attack_row,
    combined_sort_key,
    normal_row,
    validate_combined,
)

__all__ = ["FeederDataset", "FeederDatasetStats", "generate_feeder_dataset"]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class FeederDataset:
    """One feeder's complete, deterministic combined dataset (Task H)."""

    feeder_id: str
    scenario_id: str
    normal_events: Tuple[Mapping[str, object], ...]
    attack_events: Tuple[Mapping[str, object], ...]
    ground_truth_normal: Mapping[str, object]
    ground_truth_attacks: Tuple[Mapping[str, object], ...]
    failed_scenarios: Mapping[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def combined_rows(self) -> Tuple[Mapping[str, object], ...]:
        """Deterministic combined 25-column stream (``combined_sort_key``)."""
        return tuple(
            sorted(
                (*self.normal_rows_combined(), *self.attack_rows_combined()),
                key=combined_sort_key,
            )
        )

    def normal_rows_combined(self) -> Tuple[Mapping[str, object], ...]:
        return tuple(normal_row(e, feeder_id=self.feeder_id) for e in self.normal_events)

    def attack_rows_combined(self) -> Tuple[Mapping[str, object], ...]:
        rows: List[Mapping[str, object]] = []
        for gt in self.ground_truth_attacks:
            attack_id = str(gt.get(ATTACK_ID_COLUMN, ""))
            mitre = str(gt.get(MITRE_TECHNIQUE_COLUMN, ""))
            for event in gt.get("events", ()):
                rows.append(attack_row(
                    event,
                    feeder_id=self.feeder_id,
                    attack_id=attack_id,
                    mitre_technique=mitre,
                ))
        return tuple(rows)

    def counts(self) -> Mapping[str, int]:
        return {
            "normal_events": len(self.normal_events),
            "attack_events": sum(
                len(gt.get("events", ())) for gt in self.ground_truth_attacks
            ),
            "successful_scenarios": len(self.ground_truth_attacks),
            "failed_scenarios": len(self.failed_scenarios),
            "combined_rows": len(self.combined_rows()),
        }


def generate_feeder_dataset(
    feeder_id: str,
    *,
    seed: int = 42,
    results_dir: Path,
    events_csv: Optional[Path] = None,
    model: Optional[object] = None,
) -> FeederDataset:
    """Generate one feeder's combined Task H dataset, deterministically.

    Fail-closed: missing Phase E CSV, any scenario that cannot reach SUCCESS, or
    any combined-validation violation raises instead of producing a partial or
    fabricated artifact.

    The Phase E normal CSV is read-only (never modified).  Each registered
    scenario runs once through ``AttackEngine`` with a fresh, explicit
    ``AttackContext`` (model, normal-events path, reference timestamp,
    seed).  Successful scenarios contribute their real, deterministic events
    and ground truth verbatim; failed scenarios are recorded with their real
    failure reason and contribute nothing.
    """
    if events_csv is None:
        events_csv = results_dir / feeder_id / f"normal_{feeder_id}_network_events.csv"
    if not events_csv.exists():
        raise DatasetGenerationError(
            f"missing Phase E normal network events CSV: {events_csv}"
        )

    normal_events: List[Mapping[str, object]] = []
    with events_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            normal_events.append(dict(row))
    if not normal_events:
        raise DatasetGenerationError(f"Phase E normal CSV is empty: {events_csv}")

    normal_sha = _sha256_file(events_csv)
    loader = FeederLoader()
    model = model if model is not None else loader.load(feeder_id)

    engine = AttackEngine(seed=seed)
    ground_truth_attacks: List[Mapping[str, object]] = []
    failed: Dict[str, str] = {}
    for attack_id in sorted(REGISTERED_SCENARIOS):
        attack = REGISTERED_SCENARIOS[attack_id]
        context = AttackContext(
            scenario_id=scenario_id_for(attack.attack_id, feeder_id),
            feeder_id=feeder_id,
            model=model,
            event_path=events_csv,
            timestamp="2010-07-01T00:00:00",
            random_seed=seed,
            config={},
        )
        result = engine.run(attack, context)
        if result.result != RESULT_SUCCESS:
            failed[str(result.attack_id or attack_id)] = str(
                result.failure_reason or result.result
            )
            continue
        gt = result.ground_truth()
        mitre_ids = str(gt.get("mitre_technique_ids", "")).split(";")
        mitre = mitre_ids[0] if mitre_ids else ""
        record = dict(gt)
        record[ATTACK_ID_COLUMN] = str(gt.get("attack_id", ""))
        record[MITRE_TECHNIQUE_COLUMN] = mitre
        record["events"] = result.event_rows()
        record["event_count"] = len(record["events"])
        record["physical_effect"] = dict(result.physical_effect)
        record["as_dict"] = result.as_dict()
        ground_truth_attacks.append(record)

    combined = FeederDataset(
        feeder_id=feeder_id,
        scenario_id=f"combined_{feeder_id}",
        normal_events=tuple(normal_events),
        attack_events=tuple(
            e for gt in ground_truth_attacks for e in gt.get("events", ())
        ),
        ground_truth_normal={
            "feeder_id": feeder_id,
            "scenario_id": f"normal_{feeder_id}",
            "label": LABEL_NORMAL,
            "source_sha256": normal_sha,
            "normal_event_count": len(normal_events),
        },
        ground_truth_attacks=tuple(ground_truth_attacks),
        failed_scenarios=failed,
    )

    combined_rows = combined.combined_rows()
    validate_combined(
        combined_rows,
        feeder_id=feeder_id,
        expected_scenario_ids=[
            scenario_id_for(aid, feeder_id) for aid in sorted(REGISTERED_SCENARIOS)
        ],
        expected_attack_ids=sorted(REGISTERED_SCENARIOS),
    )
    return combined
