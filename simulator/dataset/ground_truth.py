"""Task I: ground-truth validation for the combined CPS attack dataset.

The DoD is: *every attack event has the correct MITRE label and ground-truth
flag*.  This module verifies that claim for the artifacts the existing Task H
pipeline already produces (combined CSV + ground-truth JSON + manifest) against
the existing F/G registries (``REGISTERED_SCENARIOS``, ``scenario_id_for``) and
the per-attack ``AttackResult`` ground truth stored verbatim in the ``as_dict``
records -- without re-running the engine and without modifying any attack
logic, dataset schema, timestamp or MITRE mapping.

Validation is report-based and fail-open-forbidden: every function returns a
structured report whose ``validation_status`` is ``PASS`` only when *every*
check passed, and otherwise ``FAIL`` with specific, machine-readable error
codes (``MISSING_MITRE_LABEL``, ``INVALID_ATTACK_LABEL``,
``MISSING_GROUND_TRUTH``, ``INVALID_SCENARIO_ID``, ``INVALID_ATTACK_ID``,
``EVENT_OUTSIDE_ATTACK_WINDOW``, ``INVALID_TIME_WINDOW``,
``MULTI_STEP_ORDER_ERROR``, ``GROUND_TRUTH_MISMATCH``).  Invalid data is never
repaired -- it is reported and the run fails closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from simulator.attack.events import ATTACK_EVENT_COLUMNS
from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for

from .errors import DatasetValidationError
from .schema import (
    ATTACK_ID_COLUMN,
    LABEL_ATTACK,
    LABEL_COLUMN,
    LABEL_NORMAL,
    MITRE_TECHNIQUE_COLUMN,
)

__all__ = [
    "STATUS_PASS",
    "STATUS_FAIL",
    "ERROR_CODES",
    "MISSING_MITRE_LABEL",
    "INVALID_ATTACK_LABEL",
    "MISSING_GROUND_TRUTH",
    "INVALID_SCENARIO_ID",
    "INVALID_ATTACK_ID",
    "EVENT_OUTSIDE_ATTACK_WINDOW",
    "INVALID_TIME_WINDOW",
    "MULTI_STEP_ORDER_ERROR",
    "GROUND_TRUTH_MISMATCH",
    "ScenarioGroundTruthResult",
    "GroundTruthValidationReport",
    "validate_scenario_ground_truth",
    "validate_feeder_ground_truth",
    "aggregate_reports",
    "write_validation_report",
]

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"

# --------------------------------------------------------------------------- #
# validation error codes (the DoD's machine-readable failure vocabulary)
# --------------------------------------------------------------------------- #
MISSING_MITRE_LABEL = "MISSING_MITRE_LABEL"
INVALID_ATTACK_LABEL = "INVALID_ATTACK_LABEL"
MISSING_GROUND_TRUTH = "MISSING_GROUND_TRUTH"
INVALID_SCENARIO_ID = "INVALID_SCENARIO_ID"
INVALID_ATTACK_ID = "INVALID_ATTACK_ID"
EVENT_OUTSIDE_ATTACK_WINDOW = "EVENT_OUTSIDE_ATTACK_WINDOW"
INVALID_TIME_WINDOW = "INVALID_TIME_WINDOW"
MULTI_STEP_ORDER_ERROR = "MULTI_STEP_ORDER_ERROR"
GROUND_TRUTH_MISMATCH = "GROUND_TRUTH_MISMATCH"

ERROR_CODES = (
    MISSING_MITRE_LABEL,
    INVALID_ATTACK_LABEL,
    MISSING_GROUND_TRUTH,
    INVALID_SCENARIO_ID,
    INVALID_ATTACK_ID,
    EVENT_OUTSIDE_ATTACK_WINDOW,
    INVALID_TIME_WINDOW,
    MULTI_STEP_ORDER_ERROR,
    GROUND_TRUTH_MISMATCH,
)


def _error(code: str, message: str) -> Tuple[str, str]:
    return (code, message)


def _expected_mitre(attack_id: str) -> Tuple[str, str]:
    """(``;``-joined ids, primary id) declared by the registered scenario."""
    attack = REGISTERED_SCENARIOS.get(attack_id)
    if attack is None or not attack.mitre:
        return ("", "")
    return (
        ";".join(m.technique_id for m in attack.mitre),
        attack.mitre[0].technique_id,
    )


def _rows_for_attack(
    rows: Sequence[Mapping[str, object]],
    attack_id: str,
) -> List[Mapping[str, object]]:
    return [
        r
        for r in rows
        if str(r.get(LABEL_COLUMN, "")) == LABEL_ATTACK
        and str(r.get(ATTACK_ID_COLUMN, "")) == attack_id
    ]


def _same_event(row: Mapping[str, object], event: Mapping[str, object]) -> bool:
    """ATTACK_EVENT_COLUMNS equality (row == AttackResult.event_rows() dict)."""
    return all(
        str(row.get(col, "")) == str(event.get(col, ""))
        for col in ATTACK_EVENT_COLUMNS
    )


def _merge_checks(*groups: Mapping[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for group in groups:
        for name, outcome in group.items():
            if outcome == STATUS_FAIL or name not in merged:
                merged[name] = outcome
    return merged


def _has_stages(metadata: Mapping[str, object]) -> bool:
    return bool(metadata.get("_stage_events")) and bool(metadata.get("stages"))


# --------------------------------------------------------------------------- #
# per-scenario validation
# --------------------------------------------------------------------------- #
def validate_scenario_ground_truth(
    *,
    feeder_id: str,
    record: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    declared_window: Optional[Tuple[str, str]] = None,
) -> "ScenarioGroundTruthResult":
    """Validate one ground-truth attack record against the exported rows.

    ``rows`` is the full combined-row stream (the record's own ATTACK rows are
    filtered out by ``attack_id``).  ``declared_window`` (``(start, end)``) is
    an optional explicit time window to also validate against when the
    architecture exposes one; by default the window is derived from the
    actual generated attack event timestamps (start = min, end = max).
    """
    errors: List[Tuple[str, str]] = []
    warnings: List[str] = []
    checks: Dict[str, str] = {}

    attack_id = str(record.get("attack_id", ""))
    scenario_id = str(record.get("scenario_id", ""))
    attack_name = str(record.get("attack_name", "") or attack_id)
    expected_scenario = scenario_id_for(attack_id, feeder_id)
    expected_ids, expected_primary = _expected_mitre(attack_id)

    # ---- metadata: traceable back to the original AttackResult ---------- #
    checks["metadata"] = STATUS_PASS
    as_dict = record.get("as_dict")
    metadata: Mapping[str, object] = {}
    if not isinstance(as_dict, dict):
        errors.append(_error(
            MISSING_GROUND_TRUTH,
            f"attack {attack_id!r}: AttackResult.as_dict missing, metadata not "
            "traceable to the engine",
        ))
        checks["metadata"] = STATUS_FAIL
    else:
        metadata = as_dict.get("metadata") if isinstance(as_dict.get("metadata"), dict) else {}
        if not metadata:
            errors.append(_error(
                MISSING_GROUND_TRUTH,
                f"attack {attack_id!r}: AttackResult metadata missing",
            ))
            checks["metadata"] = STATUS_FAIL
    record_events = record.get("events", ())
    if not isinstance(record_events, (list, tuple)) or not record_events:
        errors.append(_error(
            MISSING_GROUND_TRUTH,
            f"attack {attack_id!r}: no ground-truth events",
        ))
        checks["metadata"] = STATUS_FAIL
        record_events = ()
    record_events = list(record_events)

    # ---- scenario id / attack id ---------------------------------------- #
    checks["scenario_id"] = STATUS_PASS
    if scenario_id != expected_scenario:
        errors.append(_error(
            INVALID_SCENARIO_ID,
            f"attack {attack_id!r}: scenario_id {scenario_id!r} != expected "
            f"{expected_scenario!r}",
        ))
        checks["scenario_id"] = STATUS_FAIL

    checks["attack_id"] = STATUS_PASS
    if not attack_id:
        errors.append(_error(INVALID_ATTACK_ID, "attack record missing attack_id"))
        checks["attack_id"] = STATUS_FAIL
    elif attack_id not in REGISTERED_SCENARIOS:
        errors.append(_error(
            INVALID_ATTACK_ID,
            f"attack_id {attack_id!r} not in REGISTERED_SCENARIOS",
        ))
        checks["attack_id"] = STATUS_FAIL

    attack_rows = _rows_for_attack(rows, attack_id)

    # ---- MITRE labels ----------------------------------------------------- #
    checks["mitre"] = STATUS_PASS
    validate_mitre = bool(expected_ids)
    if validate_mitre:
        got_ids = str(record.get("mitre_technique_ids", ""))
        if got_ids != expected_ids:
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: ground truth mitre_technique_ids "
                f"{got_ids!r} != registered {expected_ids!r}",
            ))
            checks["mitre"] = STATUS_FAIL
    for row in attack_rows:
        mitre = str(row.get(MITRE_TECHNIQUE_COLUMN, ""))
        if not mitre:
            errors.append(_error(
                MISSING_MITRE_LABEL,
                f"attack {attack_id!r}: ATTACK row {row.get('event_id', '')!r} "
                "has no mitre_technique",
            ))
            checks["mitre"] = STATUS_FAIL
        elif validate_mitre and mitre != expected_primary:
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: row mitre_technique {mitre!r} != "
                f"registered primary {expected_primary!r}",
            ))
            checks["mitre"] = STATUS_FAIL

    # ---- labels ------------------------------------------------------------ #
    checks["labels"] = STATUS_PASS
    for row in attack_rows:
        if str(row.get(LABEL_COLUMN, "")) != LABEL_ATTACK:
            errors.append(_error(
                INVALID_ATTACK_LABEL,
                f"attack {attack_id!r}: event {row.get('event_id', '')!r} is not "
                "labelled ATTACK",
            ))
            checks["labels"] = STATUS_FAIL

    # ---- counts ------------------------------------------------------------- #
    checks["cross_consistency"] = STATUS_PASS
    if len(attack_rows) != len(record_events):
        errors.append(_error(
            GROUND_TRUTH_MISMATCH,
            f"attack {attack_id!r}: {len(attack_rows)} exported ATTACK rows != "
            f"{len(record_events)} ground-truth events",
        ))
        checks["cross_consistency"] = STATUS_FAIL
    if validate_mitre:
        declared_count = record.get("event_count")
        if declared_count is not None and int(declared_count) != len(record_events):
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: event_count {declared_count} != "
                f"{len(record_events)} events",
            ))
            checks["cross_consistency"] = STATUS_FAIL

    # ---- exported vs AttackResult events + traceability ---------------------- #
    row_by_event_id = {str(r.get("event_id", "")): r for r in attack_rows}
    for event in record_events:
        event_id = str(event.get("event_id", ""))
        row = row_by_event_id.get(event_id)
        if row is None:
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: exported row missing for event "
                f"{event_id!r}",
            ))
            checks["cross_consistency"] = STATUS_FAIL
            continue
        if not _same_event(row, event):
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: exported row differs from AttackResult "
                f"event {event_id!r}",
            ))
            checks["cross_consistency"] = STATUS_FAIL
    for event_id, row in row_by_event_id.items():
        if not event_id:
            errors.append(_error(
                INVALID_ATTACK_ID,
                f"attack {attack_id!r}: ATTACK row missing event_id",
            ))
            checks["cross_consistency"] = STATUS_FAIL
        elif validate_mitre and not event_id.startswith(f"{expected_scenario}-atk"):
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: event_id {event_id!r} not traceable to "
                f"scenario {expected_scenario!r}",
            ))
            checks["cross_consistency"] = STATUS_FAIL

    # ---- time window (derived from the generated timestamps, never fixed) ---- #
    ts_list = [str(r.get("timestamp", "")) for r in attack_rows]
    window_start = ""
    window_end = ""
    checks["window"] = STATUS_PASS
    if not ts_list:
        errors.append(_error(
            INVALID_TIME_WINDOW,
            f"attack {attack_id!r}: no exported events to derive a time window",
        ))
        checks["window"] = STATUS_FAIL
    else:
        window_start = min(ts_list)
        window_end = max(ts_list)
        if not window_start or not window_end:
            errors.append(_error(
                INVALID_TIME_WINDOW,
                f"attack {attack_id!r}: attack event missing a timestamp",
            ))
            checks["window"] = STATUS_FAIL
        if window_start > window_end:
            errors.append(_error(
                INVALID_TIME_WINDOW,
                f"attack {attack_id!r}: attack window start {window_start!r} > "
                f"end {window_end!r}",
            ))
            checks["window"] = STATUS_FAIL
        gt_ts = [str(e.get("timestamp", "")) for e in record_events]
        gt_start = min(gt_ts) if gt_ts else ""
        gt_end = max(gt_ts) if gt_ts else ""
        for ts in ts_list:
            if ts and gt_ts and (ts < gt_start or ts > gt_end):
                errors.append(_error(
                    EVENT_OUTSIDE_ATTACK_WINDOW,
                    f"attack {attack_id!r}: event timestamp {ts!r} outside "
                    f"ground-truth window {gt_start!r}..{gt_end!r}",
                ))
                checks["window"] = STATUS_FAIL
        if declared_window is not None:
            declared_start, declared_end = declared_window
            if declared_start > declared_end:
                errors.append(_error(
                    INVALID_TIME_WINDOW,
                    f"attack {attack_id!r}: declared window start "
                    f"{declared_start!r} > end {declared_end!r}",
                ))
                checks["window"] = STATUS_FAIL
            for ts in ts_list:
                if ts and (ts < declared_start or ts > declared_end):
                    errors.append(_error(
                        EVENT_OUTSIDE_ATTACK_WINDOW,
                        f"attack {attack_id!r}: event timestamp {ts!r} outside "
                        f"declared window {declared_start!r}..{declared_end!r}",
                    ))
                    checks["window"] = STATUS_FAIL

    # ---- ground-truth / injected semantics ------------------------------------ #
    checks["ground_truth_flag"] = STATUS_PASS
    for event in record_events:
        if str(event.get("message_type", "")) in ("REPORT", "COMMAND"):
            if str(event.get("injected", "")) not in ("0", "1"):
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    f"attack {attack_id!r}: event {str(event.get('event_id', ''))!r} "
                    "has a non-flagged injected state",
                ))
                checks["ground_truth_flag"] = STATUS_FAIL

    # scenario-specific documented invariants (validated, never repaired)
    if attack_id == "false_measurement":
        reports = [e for e in record_events if str(e.get("message_type", "")) == "REPORT"]
        for ev in reports:
            original = str(ev.get("original_value", ""))
            reported = str(ev.get("reported_value", ""))
            if not original or not reported or original == reported:
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    "false_measurement: original_value and reported_value must "
                    "both exist and stay distinguishable (physical truth vs "
                    "cyber truth)",
                ))
                checks["ground_truth_flag"] = STATUS_FAIL
            if str(ev.get("injected", "")) != "1":
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    "false_measurement: forged REPORT must be injected",
                ))
                checks["ground_truth_flag"] = STATUS_FAIL
    if attack_id == "communication_disruption":
        reports = [e for e in record_events if str(e.get("message_type", "")) == "REPORT"]
        if not any(str(e.get("delivery_status", "")) == "DROPPED" for e in reports):
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                "communication_disruption: blocked REPORT must carry "
                "delivery_status=DROPPED",
            ))
            checks["ground_truth_flag"] = STATUS_FAIL
        if str(metadata.get("delivery_status", "")) != "DROPPED":
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                "communication_disruption: metadata must record "
                "delivery_status=DROPPED",
            ))
            checks["ground_truth_flag"] = STATUS_FAIL

    # ---- multi-stage chronology (any scenario, detected via its metadata) ----- #
    checks["multi_step_chronology"] = STATUS_PASS
    if _has_stages(metadata):
        stage_events = list(metadata.get("_stage_events", ()))
        stages = list(metadata.get("stages", ()))
        if len(stage_events) != len(stages):
            errors.append(_error(
                MULTI_STEP_ORDER_ERROR,
                f"attack {attack_id!r}: {len(stage_events)} stage event groups "
                f"!= {len(stages)} stage records",
            ))
            checks["multi_step_chronology"] = STATUS_FAIL
        stage_ts: List[str] = []
        for index, group in enumerate(stage_events):
            stamps = {str(e.get("timestamp", "")) for e in group}
            if not stamps or "" in stamps or len(stamps) != 1:
                errors.append(_error(
                    MULTI_STEP_ORDER_ERROR,
                    f"attack {attack_id!r}: stage {index} events span "
                    f"timestamps {sorted(stamps)}",
                ))
                checks["multi_step_chronology"] = STATUS_FAIL
            stage_ts.append(
                next(iter(stamps)) if len(stamps) == 1 and "" not in stamps else ""
            )
            if index < len(stages):
                stage_count = stages[index].get("event_count")
                if stage_count is not None and int(stage_count) != len(group):
                    errors.append(_error(
                        MULTI_STEP_ORDER_ERROR,
                        f"attack {attack_id!r}: stage {index} event_count "
                        f"{stage_count} != {len(group)} stage events",
                    ))
                    checks["multi_step_chronology"] = STATUS_FAIL
        for index in range(1, len(stage_ts)):
            if stage_ts[index] and stage_ts[index] <= stage_ts[index - 1]:
                errors.append(_error(
                    MULTI_STEP_ORDER_ERROR,
                    f"attack {attack_id!r}: stage timestamps not strictly "
                    f"chronological: {stage_ts}",
                ))
                checks["multi_step_chronology"] = STATUS_FAIL

    # ---- exported timeline non-decreasing (chronology) -------------------------- #
    for index in range(1, len(ts_list)):
        if ts_list[index] < ts_list[index - 1]:
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"attack {attack_id!r}: exported timeline not chronological: "
                f"{ts_list[index - 1]!r} -> {ts_list[index]!r}",
            ))
            checks["cross_consistency"] = STATUS_FAIL

    status = STATUS_PASS if not errors else STATUS_FAIL
    return ScenarioGroundTruthResult(
        feeder_id=feeder_id,
        attack_id=attack_id,
        scenario_id=scenario_id,
        attack_name=attack_name,
        result=str(record.get("result", "")),
        status=status,
        event_count=len(record_events),
        mitre_technique=str(record.get(MITRE_TECHNIQUE_COLUMN, "")) or expected_primary,
        mitre_technique_ids=str(record.get("mitre_technique_ids", "")),
        window_start=window_start,
        window_end=window_end,
        target_point_id=str(record.get("target_point_id", "")),
        physical_effect_status=str(
            (record.get("physical_effect") or {}).get("status", "")
        ),
        event_ids=tuple(str(e.get("event_id", "")) for e in record_events),
        stages=tuple(
            dict(s)
            for s in (
                metadata.get("stages", ())
                if isinstance(metadata.get("stages", ()), (list, tuple))
                else ()
            )
        ),
        checks=checks,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class ScenarioGroundTruthResult:
    """Per-attack ground-truth validation outcome (metadata + validation)."""

    feeder_id: str
    attack_id: str
    scenario_id: str
    attack_name: str
    result: str
    status: str
    event_count: int
    mitre_technique: str
    mitre_technique_ids: str
    window_start: str
    window_end: str
    target_point_id: str
    physical_effect_status: str
    event_ids: Tuple[str, ...]
    stages: Tuple[Mapping[str, object], ...]
    checks: Mapping[str, str]
    errors: Tuple[Tuple[str, str], ...]
    warnings: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "feeder_id": self.feeder_id,
            "attack_id": self.attack_id,
            "scenario_id": self.scenario_id,
            "attack_name": self.attack_name,
            "result": self.result,
            "validation_status": self.status,
            "event_count": self.event_count,
            "mitre_technique": self.mitre_technique,
            "mitre_technique_ids": self.mitre_technique_ids,
            "attack_window_start": self.window_start,
            "attack_window_end": self.window_end,
            "target_point_id": self.target_point_id,
            "physical_effect_status": self.physical_effect_status,
            "event_ids": list(self.event_ids),
            "stages": [dict(s) for s in self.stages],
            "checks": dict(self.checks),
            "errors": [{"code": code, "message": message} for code, message in self.errors],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class GroundTruthValidationReport:
    """One feeder's aggregated ground-truth validation report."""

    feeder_id: str
    validation_status: str
    generated_at: str
    total_events: int
    normal_events: int
    attack_events: int
    checks: Mapping[str, str]
    scenarios: Tuple["ScenarioGroundTruthResult", ...]
    errors: Tuple[Tuple[str, str], ...]
    warnings: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "feeder_id": self.feeder_id,
            "validation_status": self.validation_status,
            "generated_at": self.generated_at,
            "total_events": self.total_events,
            "normal_events": self.normal_events,
            "attack_events": self.attack_events,
            "checks": dict(self.checks),
            "scenarios": [s.as_dict() for s in self.scenarios],
            "errors": [{"code": code, "message": message} for code, message in self.errors],
            "warnings": list(self.warnings),
        }


def validate_feeder_ground_truth(
    *,
    feeder_id: str,
    combined_rows: Sequence[Mapping[str, object]],
    ground_truth: Mapping[str, object],
    manifest: Optional[Mapping[str, object]] = None,
) -> GroundTruthValidationReport:
    """Validate one feeder's full ground-truth artifact set, fail-closed.

    Checks the exported combined rows against the ground-truth JSON (and, when
    present, the manifest): counts, labels, MITRE labels, the ``injected``
    ground-truth flag, per-scenario attack metadata/time-windows and the
    AttackResult <-> exported-event cross consistency.
    """
    errors: List[Tuple[str, str]] = []
    warnings: List[str] = []
    checks: Dict[str, str] = {}

    # ---- structure ------------------------------------------------------- #
    checks["ground_truth_present"] = STATUS_PASS
    if not ground_truth:
        errors.append(_error(MISSING_GROUND_TRUTH, "ground-truth JSON is empty"))
        checks["ground_truth_present"] = STATUS_FAIL
    if str(ground_truth.get("feeder_id", "")) != feeder_id:
        errors.append(_error(
            GROUND_TRUTH_MISMATCH,
            f"ground-truth feeder_id {ground_truth.get('feeder_id')!r} != "
            f"declared {feeder_id!r}",
        ))
        checks["ground_truth_present"] = STATUS_FAIL
    normal_gt = ground_truth.get("normal") if isinstance(ground_truth.get("normal"), dict) else {}
    if not normal_gt:
        errors.append(_error(MISSING_GROUND_TRUTH, "missing NORMAL ground truth"))
        checks["ground_truth_present"] = STATUS_FAIL
    attacks = ground_truth.get("attacks", ())
    if not isinstance(attacks, (list, tuple)) or not attacks:
        errors.append(_error(MISSING_GROUND_TRUTH, "missing attack ground truth"))
        checks["ground_truth_present"] = STATUS_FAIL
        attacks = ()
    attacks = list(attacks)

    normal_rows = [
        r for r in combined_rows if str(r.get(LABEL_COLUMN, "")) == LABEL_NORMAL
    ]
    attack_rows = [
        r for r in combined_rows if str(r.get(LABEL_COLUMN, "")) == LABEL_ATTACK
    ]

    # ---- counts ----------------------------------------------------------- #
    checks["counts"] = STATUS_PASS
    expected_normal = int(normal_gt.get("normal_event_count", 0) or 0)
    if len(normal_rows) != expected_normal:
        errors.append(_error(
            GROUND_TRUTH_MISMATCH,
            f"{len(normal_rows)} NORMAL rows != ground-truth "
            f"{expected_normal}",
        ))
        checks["counts"] = STATUS_FAIL
    expected_attack = sum(
        int(str(a.get("event_count", "")) or 0) for a in attacks
    )
    if len(attack_rows) != expected_attack:
        errors.append(_error(
            GROUND_TRUTH_MISMATCH,
            f"{len(attack_rows)} ATTACK rows != ground-truth {expected_attack}",
        ))
        checks["counts"] = STATUS_FAIL

    # ---- labels / normal rows --------------------------------------------- #
    checks["labels"] = STATUS_PASS
    normal_scenario = str(normal_gt.get("scenario_id", "")) or f"normal_{feeder_id}"
    for row in normal_rows:
        if str(row.get(ATTACK_ID_COLUMN, "")) or str(row.get(MITRE_TECHNIQUE_COLUMN, "")):
            errors.append(_error(
                INVALID_ATTACK_LABEL,
                f"NORMAL row {row.get('event_id', '')!r} carries attack metadata",
            ))
            checks["labels"] = STATUS_FAIL
        if str(row.get("injected", "")).lower() in ("1", "true"):
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"NORMAL row {row.get('event_id', '')!r} marked as injected",
            ))
            checks["labels"] = STATUS_FAIL
        if str(row.get("scenario_id", "")) != normal_scenario:
            errors.append(_error(
                INVALID_SCENARIO_ID,
                f"NORMAL row scenario_id {row.get('scenario_id')!r} != "
                f"{normal_scenario!r}",
            ))
            checks["labels"] = STATUS_FAIL
    for row in attack_rows:
        if not str(row.get(ATTACK_ID_COLUMN, "")):
            errors.append(_error(
                INVALID_ATTACK_ID,
                f"ATTACK row {row.get('event_id', '')!r} missing attack_id",
            ))
            checks["labels"] = STATUS_FAIL
        if not str(row.get(MITRE_TECHNIQUE_COLUMN, "")):
            errors.append(_error(
                MISSING_MITRE_LABEL,
                f"ATTACK row {row.get('event_id', '')!r} missing mitre_technique",
            ))
            checks["mitre"] = STATUS_FAIL

    # ---- failed scenarios must contribute no phantom rows ------------------- #
    failed_scenarios = ground_truth.get("failed_scenarios", {})
    if isinstance(failed_scenarios, dict):
        for failed_id in failed_scenarios:
            if any(str(r.get(ATTACK_ID_COLUMN, "")) == failed_id for r in attack_rows):
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    f"failed scenario {failed_id!r} still has exported ATTACK rows",
                ))
                checks["cross_consistency"] = STATUS_FAIL

    # ---- every ATTACK row must map to a real ground-truth record -------------- #
    recorded = {str(a.get("attack_id", "")) for a in attacks}
    checks.setdefault("cross_consistency", STATUS_PASS)
    for row in attack_rows:
        aid = str(row.get(ATTACK_ID_COLUMN, ""))
        if not aid:
            continue
        if aid not in recorded:
            errors.append(_error(
                GROUND_TRUTH_MISMATCH,
                f"ATTACK row {row.get('event_id', '')!r} (attack_id {aid!r}) has "
                "no ground-truth record",
            ))
            checks["cross_consistency"] = STATUS_FAIL
        elif aid in REGISTERED_SCENARIOS:
            _, primary = _expected_mitre(aid)
            mitre = str(row.get(MITRE_TECHNIQUE_COLUMN, ""))
            if primary and mitre and mitre != primary:
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    f"ATTACK row {row.get('event_id', '')!r} mitre_technique "
                    f"{mitre!r} != registered primary {primary!r}",
                ))
                checks.setdefault("mitre", STATUS_PASS)
                checks["mitre"] = STATUS_FAIL

    # ---- per-scenario validation --------------------------------------------- #
    scenario_results = [
        validate_scenario_ground_truth(
            feeder_id=feeder_id,
            record=record,
            rows=combined_rows,
        )
        for record in attacks
    ]
    for scenario in scenario_results:
        if scenario.status == STATUS_FAIL:
            for code, message in scenario.errors:
                errors.append((code, f"{feeder_id}/{scenario.attack_id}: {message}"))

    # ---- manifest cross-check (when the manifest is present) ------------------ #
    if manifest:
        for scenario in manifest.get("scenarios", ()):
            aid = str(scenario.get("attack_id", ""))
            status_token = str(scenario.get("status", ""))
            if status_token == "success":
                declared = int(scenario.get("event_count", 0) or 0)
                actual = sum(
                    1
                    for r in attack_rows
                    if str(r.get(ATTACK_ID_COLUMN, "")) == aid
                )
                if declared != actual:
                    errors.append(_error(
                        GROUND_TRUTH_MISMATCH,
                        f"manifest scenario {aid!r} event_count {declared} != "
                        f"{actual} exported rows",
                    ))
                    checks.setdefault("cross_consistency", STATUS_PASS)
                    checks["cross_consistency"] = STATUS_FAIL
            if status_token == "failed" and any(
                str(r.get(ATTACK_ID_COLUMN, "")) == aid for r in attack_rows
            ):
                errors.append(_error(
                    GROUND_TRUTH_MISMATCH,
                    f"manifest marks scenario {aid!r} failed but rows were exported",
                ))
                checks.setdefault("cross_consistency", STATUS_PASS)
                checks["cross_consistency"] = STATUS_FAIL

    merged_checks = _merge_checks(
        checks,
        *(scenario.checks for scenario in scenario_results),
    )
    return GroundTruthValidationReport(
        feeder_id=feeder_id,
        validation_status=STATUS_PASS if not errors else STATUS_FAIL,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_events=len(combined_rows),
        normal_events=len(normal_rows),
        attack_events=len(attack_rows),
        checks=merged_checks,
        scenarios=tuple(scenario_results),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# report aggregation + writer
# --------------------------------------------------------------------------- #
def aggregate_reports(
    reports: Sequence[GroundTruthValidationReport],
) -> Mapping[str, object]:
    """Compact machine-readable aggregate of the per-feeder reports."""
    checks = _merge_checks(*(report.checks for report in reports))
    errors: List[Dict[str, object]] = []
    warnings: List[str] = []
    for report in reports:
        for code, message in report.errors:
            errors.append({"code": code, "message": message, "feeder_id": report.feeder_id})
        warnings.extend(report.warnings)
    return {
        "validation_status": (
            STATUS_PASS
            if reports and all(r.validation_status == STATUS_PASS for r in reports)
            else STATUS_FAIL
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feeders": [report.as_dict() for report in reports],
        "scenarios": [
            scenario
            for report in reports
            for scenario in report.as_dict()["scenarios"]
        ],
        "total_events": sum(r.total_events for r in reports),
        "attack_events": sum(r.attack_events for r in reports),
        "normal_events": sum(r.normal_events for r in reports),
        "checks": dict(checks),
        "errors": errors,
        "warnings": warnings,
    }


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_validation_report(
    reports: Sequence[GroundTruthValidationReport],
    *,
    results_dir: Path,
) -> Tuple[Path, str]:
    """Write ``<results_dir>/ground_truth/validation.json`` (fail-closed)."""
    if not reports:
        raise DatasetValidationError(
            "cannot write a ground-truth validation report with no feeder reports"
        )
    data = aggregate_reports(reports)
    out = results_dir / "ground_truth" / "validation.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        raise DatasetValidationError(
            f"cannot write validation report {out}: {exc}"
        ) from exc
    return out, _sha256_file(out)