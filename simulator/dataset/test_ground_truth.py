"""Task I tests: ground-truth validation of the combined attack dataset.

The DoD under test: *every attack event has the correct MITRE label and
ground-truth flag*.  These tests exercise the report-based, fail-closed
``simulator.dataset.ground_truth`` module against synthetic-but-canonical
AttackResult-shaped records and combined rows (built with the canonical
``attack_row``/``normal_row`` builders and the real registered MITRE
mappings).  No engine, no OpenDSS, deterministic by construction.

Coverage follows the Task I checklist: attack metadata, attack labels, MITRE
labels, ground-truth flag, false-measurement / communication-disruption /
multi-step invariants, time windows, cross-consistency, aggregate + writer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for
from simulator.dataset.ground_truth import (
    ERROR_CODES,
    EVENT_OUTSIDE_ATTACK_WINDOW,
    GROUND_TRUTH_MISMATCH,
    INVALID_ATTACK_ID,
    INVALID_ATTACK_LABEL,
    INVALID_SCENARIO_ID,
    INVALID_TIME_WINDOW,
    MISSING_GROUND_TRUTH,
    MISSING_MITRE_LABEL,
    MULTI_STEP_ORDER_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    aggregate_reports,
    validate_feeder_ground_truth,
    validate_scenario_ground_truth,
    write_validation_report,
)
from simulator.dataset.schema import (
    ATTACK_ID_COLUMN,
    LABEL_ATTACK,
    LABEL_COLUMN,
    LABEL_NORMAL,
    MITRE_TECHNIQUE_COLUMN,
    attack_row,
    normal_row,
)
from simulator.network.events import rtu_of

TS = "2010-07-01T00:00:00"
F = "ieee37"


# --------------------------------------------------------------------------- #
# canonical-shape helpers (verbatim string-valued, as_dict-shaped)
# --------------------------------------------------------------------------- #
def primary_mitre(attack_id: str) -> str:
    return REGISTERED_SCENARIOS[attack_id].mitre[0].technique_id


def mitre_ids(attack_id: str) -> str:
    return ";".join(m.technique_id for m in REGISTERED_SCENARIOS[attack_id].mitre)


def make_event(
    attack_id: str,
    feeder_id: str,
    seq: int,
    *,
    ts: str = TS,
    **over: object,
) -> Dict[str, object]:
    """One 22-column ``AttackEvent.as_dict()``-shaped event dict."""
    scenario = scenario_id_for(attack_id, feeder_id)
    event: Dict[str, object] = {
        "event_id": f"{scenario}-atk{seq:03d}",
        "scenario_id": scenario,
        "timestamp": ts,
        "feeder_id": feeder_id,
        "src_device": rtu_of(feeder_id),
        "dst_device": "SCADA_MASTER",
        "protocol": "DNP3",
        "message_type": "REPORT",
        "direction": "RTU_TO_SCADA",
        "sequence": str(seq),
        "point_id": "P1",
        "value": "1.0",
        "unit": "",
        "quality": "GOOD",
        "delivery_status": "DELIVERED",
        "latency_ms": "0",
        "injected": "1",
        "replay_of_event_id": "",
        "original_value": "",
        "reported_value": "",
        "command_id": "",
        "result": "",
    }
    event.update(over)
    return event


def normalize_value(value: str) -> str:
    """as_dict stores numbers with repr(); keep fixture strings verbatim."""
    return value


def make_record(
    attack_id: str,
    feeder_id: str,
    events: Sequence[Mapping[str, object]],
    **over: object,
) -> Dict[str, object]:
    """One Task I ground-truth attack record (attack_result-shaped)."""
    scenario = scenario_id_for(attack_id, feeder_id)
    event_list = [dict(e) for e in events]
    record: Dict[str, object] = {
        "scenario_id": scenario,
        "attack_id": attack_id,
        "attack_name": attack_id,
        "category": "network",
        "feeder_id": feeder_id,
        "timestamp": str(event_list[0].get("timestamp", TS)) if event_list else TS,
        "is_attack": 1,
        "result": "success",
        "status": "executed",
        "failure_reason": "",
        "target_point_id": str(event_list[0].get("point_id", "")) if event_list else "",
        "mitre_technique_ids": mitre_ids(attack_id),
        "mitre_technique": primary_mitre(attack_id),
        "original_value": str(event_list[0].get("original_value", "")) if event_list else "",
        "reported_value": str(event_list[0].get("reported_value", "")) if event_list else "",
        "command_id": "",
        "physical_effect": {"status": "computed", "deltas": {"source_p_kw": 0.0, "vpu_min": 0.0}},
        "events": event_list,
        "event_count": len(event_list),
        "as_dict": {
            "scenario_id": scenario,
            "attack_id": attack_id,
            "is_attack": 1,
            "result": "success",
            "metadata": {"trace": "fixture"},
            "events": [dict(e) for e in event_list],
            "event_count": len(event_list),
            "physical_effect": {"status": "computed", "deltas": {"source_p_kw": 0.0, "vpu_min": 0.0}},
        },
    }
    record.update(over)
    return record


def normal_event(feeder_id: str, idx: int, **over: object) -> Dict[str, object]:
    event: Dict[str, object] = {
        "event_id": f"normal_{feeder_id}-ev{idx:08d}",
        "scenario_id": f"normal_{feeder_id}",
        "timestamp": TS,
        "feeder_id": feeder_id,
        "src_device": "SCADA_MASTER",
        "dst_device": rtu_of(feeder_id),
        "protocol": "DNP3",
        "message_type": "TELEMETRY",
        "direction": "SCADA_TO_RTU",
        "sequence": str(idx),
        "point_id": "V1",
        "value": "7340.5",
        "unit": "V",
        "quality": "GOOD",
        "delivery_status": "DELIVERED",
        "latency_ms": "0",
    }
    event.update(over)
    return event


def combined_rows(
    records: Sequence[Mapping[str, object]],
    feeder_id: str,
    *,
    n_normal: int = 3,
) -> List[Mapping[str, object]]:
    """Canonical combined rows for the given ground-truth records."""
    rows: List[Mapping[str, object]] = []
    for record in records:
        aid = str(record.get("attack_id", ""))
        lead = str(record.get(MITRE_TECHNIQUE_COLUMN, "")) or primary_mitre(aid)
        for ev in record.get("events", ()):
            rows.append(attack_row(
                ev, feeder_id=feeder_id, attack_id=aid, mitre_technique=lead
            ))
    for i in range(n_normal):
        rows.append(normal_row(normal_event(feeder_id, i), feeder_id=feeder_id))
    return rows


def make_ground_truth(
    feeder_id: str,
    records: Sequence[Mapping[str, object]],
    *,
    n_normal: int = 3,
    failed: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    return {
        "feeder_id": feeder_id,
        "seed": 42,
        "scenario_id": f"combined_{feeder_id}",
        "normal": {
            "feeder_id": feeder_id,
            "scenario_id": f"normal_{feeder_id}",
            "label": LABEL_NORMAL,
            "normal_event_count": n_normal,
        },
        "attacks": [dict(r) for r in records],
        "failed_scenarios": dict(failed or {}),
    }


def codes(scenario) -> List[str]:
    return [c for c, _ in scenario.errors]


# --------------------------------------------------------------------------- #
# attack metadata
# --------------------------------------------------------------------------- #
class MetadataTests(unittest.TestCase):
    def test_record_metadata_traceable_passes(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="DISCOVERED"),
            make_event("reconnaissance", F, 2, result="READ",
                       original_value="7340.5", reported_value="7340.5"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.attack_id, "reconnaissance")
        self.assertEqual(result.scenario_id, scenario_id_for("reconnaissance", F))
        self.assertEqual(result.event_count, 2)
        self.assertEqual(result.mitre_technique_ids, "T0846")

    def test_missing_as_dict_fails_missing_ground_truth(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1),
        ], as_dict=None)
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(MISSING_GROUND_TRUTH, codes(result))

    def test_wrong_scenario_id_fails_invalid_scenario_id(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1),
        ], scenario_id="reconnaissance_other")
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(INVALID_SCENARIO_ID, codes(result))

    def test_unknown_attack_id_fails_invalid_attack_id(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1),
        ])
        record["attack_id"] = "not_a_scenario"
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(INVALID_ATTACK_ID, codes(result))

    def test_missing_attack_id_fails_invalid_attack_id(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1),
        ])
        record["attack_id"] = ""
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(INVALID_ATTACK_ID, codes(result))


# --------------------------------------------------------------------------- #
# attack labels
# --------------------------------------------------------------------------- #
class LabelTests(unittest.TestCase):
    def test_attack_event_labelled_attack_passes(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)

    def test_attack_event_marked_normal_is_detected(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F)
        for row in rows:
            if str(row.get(ATTACK_ID_COLUMN, "")) == "reconnaissance":
                row[LABEL_COLUMN] = LABEL_NORMAL
                row[ATTACK_ID_COLUMN] = ""
                row[MITRE_TECHNIQUE_COLUMN] = ""
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=rows
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertTrue(any(c in (MISSING_GROUND_TRUTH, GROUND_TRUTH_MISMATCH) for c in codes(result)))

    def test_normal_row_carrying_attack_metadata_fails_invalid_attack_label(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F, n_normal=2)
        rows.append(attack_row(
            make_event("reconnaissance", F, 2),
            feeder_id=F, attack_id="reconnaissance", mitre_technique="T0846",
        ))
        rows[-1][LABEL_COLUMN] = LABEL_NORMAL
        rows[-1][ATTACK_ID_COLUMN] = "reconnaissance"
        rows[-1][MITRE_TECHNIQUE_COLUMN] = "T0846"
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=rows,
            ground_truth=make_ground_truth(F, [record], n_normal=2),
        )
        self.assertEqual(report.validation_status, STATUS_FAIL)
        self.assertIn(INVALID_ATTACK_LABEL, [c for c, _ in report.errors])

    def test_attack_label_normal_rows_pure(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows([record], F),
            ground_truth=make_ground_truth(F, [record]),
        )
        self.assertEqual(report.validation_status, STATUS_PASS)
        for scenario in report.scenarios:
            self.assertEqual(scenario.checks["labels"], STATUS_PASS)


# --------------------------------------------------------------------------- #
# MITRE labels
# --------------------------------------------------------------------------- #
class MitreTests(unittest.TestCase):
    def test_mitre_matches_registered_technique(self):
        record = make_record("false_measurement", F, [
            make_event("false_measurement", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="READ"),
            make_event("false_measurement", F, 2, injected="1",
                       original_value="7340.5", reported_value="7030.0", result="SPOOFED"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.mitre_technique, "T0856")

    def test_missing_row_mitre_fails_missing_mitre_label(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F)
        for row in rows:
            if str(row.get(ATTACK_ID_COLUMN, "")) == "reconnaissance":
                row[MITRE_TECHNIQUE_COLUMN] = ""
        result = validate_scenario_ground_truth(feeder_id=F, record=record, rows=rows)
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(MISSING_MITRE_LABEL, codes(result))

    def test_incorrect_row_mitre_fails_ground_truth_mismatch(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F)
        for row in rows:
            if str(row.get(ATTACK_ID_COLUMN, "")) == "reconnaissance":
                row[MITRE_TECHNIQUE_COLUMN] = "T9999"
        result = validate_scenario_ground_truth(feeder_id=F, record=record, rows=rows)
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))

    def test_record_mitre_ids_mismatch_fails(self):
        record = make_record("multi_step_attack", F, [
            make_event("multi_step_attack", F, 1),
        ])
        record["mitre_technique_ids"] = "T0846;T9999"
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))# --------------------------------------------------------------------------- #
# ground-truth flag: normal rows, false measurement, communication disruption
# --------------------------------------------------------------------------- #
class GroundTruthFlagTests(unittest.TestCase):
    def test_normal_rows_never_injected_pass(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows([record], F),
            ground_truth=make_ground_truth(F, [record]),
        )
        self.assertEqual(report.validation_status, STATUS_PASS)
        self.assertEqual(report.normal_events, 3)

    def test_normal_row_marked_injected_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F, n_normal=2)
        for row in rows:
            if str(row.get(LABEL_COLUMN, "")) == LABEL_NORMAL:
                row["injected"] = "1"
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=rows,
            ground_truth=make_ground_truth(F, [record], n_normal=2),
        )
        self.assertEqual(report.validation_status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, [c for c, _ in report.errors])

    def test_false_measurement_original_reported_distinguishable(self):
        record = make_record("false_measurement", F, [
            make_event("false_measurement", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="READ"),
            make_event("false_measurement", F, 2, injected="1",
                       original_value="7340.5", reported_value="7030.0", result="SPOOFED"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.checks["ground_truth_flag"], STATUS_PASS)

    def test_false_measurement_identical_values_fail(self):
        record = make_record("false_measurement", F, [
            make_event("false_measurement", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="READ"),
            make_event("false_measurement", F, 2, injected="1",
                       original_value="7340.5", reported_value="7340.5", result="SPOOFED"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))

    def test_communication_disruption_dropped_pass(self):
        record = make_record("communication_disruption", F, [
            make_event("communication_disruption", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="BLOCK", command_id="C1"),
            make_event("communication_disruption", F, 2, injected="0",
                       delivery_status="DROPPED", latency_ms="0", result="BLOCKED"),
        ])
        record["as_dict"]["metadata"] = {"delivery_status": "DROPPED", "trace": "fixture"}
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.checks["ground_truth_flag"], STATUS_PASS)

    def test_communication_disruption_not_dropped_fail(self):
        record = make_record("communication_disruption", F, [
            make_event("communication_disruption", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="BLOCK", command_id="C1"),
            make_event("communication_disruption", F, 2, injected="0",
                       delivery_status="DELIVERED", result="BLOCKED"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))


# --------------------------------------------------------------------------- #
# multi-step stage association / chronology
# --------------------------------------------------------------------------- #
def _multi_step_record(ordered: bool = True) -> Dict[str, object]:
    st1 = [
        make_event("multi_step_attack", F, 1, ts=TS),
        make_event("multi_step_attack", F, 2, ts=TS),
    ]
    st2 = [make_event("multi_step_attack", F, 3, ts="2010-07-01T00:00:01")]
    st3 = [make_event("multi_step_attack", F, 4, ts="2010-07-01T00:00:02")]
    groups = [st1, st2, st3]
    ids = ("reconnaissance", "unauthorized_command", "parameter_modification")
    stages = [
        {"attack_id": aid, "event_count": len(group)}
        for aid, group in zip(ids, groups)
    ]
    if not ordered:
        groups = [st2, st1, st3]
        stages = [
            {"attack_id": aid, "event_count": len(group)}
            for aid, group in zip(ids, groups)
        ]
    events = [e for group in groups for e in group]
    record = make_record("multi_step_attack", F, events)
    record["as_dict"] = {
        "scenario_id": scenario_id_for("multi_step_attack", F),
        "attack_id": "multi_step_attack",
        "is_attack": 1,
        "result": "success",
        "metadata": {
            "stages": stages,
            "_stage_events": [list(group) for group in groups],
            "trace": "fixture",
        },
        "events": [dict(e) for e in events],
        "event_count": len(events),
        "physical_effect": {"status": "computed", "deltas": {"source_p_kw": 0.0, "vpu_min": 0.0}},
    }
    return record


class MultiStepTests(unittest.TestCase):
    def test_multi_step_stages_chronological_pass(self):
        record = _multi_step_record(ordered=True)
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.checks["multi_step_chronology"], STATUS_PASS)
        self.assertEqual(result.mitre_technique_ids, "T0846;T0855;T0836")
        self.assertEqual(len(result.stages), 3)

    def test_multi_step_out_of_order_fails_multi_step_order_error(self):
        record = _multi_step_record(ordered=False)
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(MULTI_STEP_ORDER_ERROR, codes(result))


# --------------------------------------------------------------------------- #
# time windows
# --------------------------------------------------------------------------- #
class TimeWindowTests(unittest.TestCase):
    def test_window_derived_from_generated_timestamps(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, ts=TS, result="READ"),
            make_event("reconnaissance", F, 2, ts="2010-07-01T00:00:01", result="READ"),
        ])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_PASS)
        self.assertEqual(result.window_start, TS)
        self.assertEqual(result.window_end, "2010-07-01T00:00:01")
        self.assertEqual(result.checks["window"], STATUS_PASS)

    def test_event_outside_declared_window_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, ts=TS, result="READ"),
        ])
        rows = combined_rows([record], F)
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=rows,
            declared_window=("2010-07-02T00:00:00", "2010-07-02T00:00:01"),
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(EVENT_OUTSIDE_ATTACK_WINDOW, codes(result))

    def test_reversed_declared_window_fails_invalid_time_window(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, ts=TS, result="READ"),
        ])
        rows = combined_rows([record], F)
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=rows,
            declared_window=("2010-07-02T00:00:01", "2010-07-02T00:00:00"),
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(INVALID_TIME_WINDOW, codes(result))


# --------------------------------------------------------------------------- #
# cross-consistency: exported rows vs AttackResult ground-truth events
# --------------------------------------------------------------------------- #
class CrossConsistencyTests(unittest.TestCase):
    def test_exported_row_differs_from_event_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([record], F)
        for row in rows:
            if str(row.get(ATTACK_ID_COLUMN, "")) == "reconnaissance":
                row["value"] = "999.0"
        result = validate_scenario_ground_truth(feeder_id=F, record=record, rows=rows)
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))

    def test_event_count_mismatch_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
            make_event("reconnaissance", F, 2, result="READ"),
        ])
        record["event_count"] = 99
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, codes(result))

    def test_record_without_events_fails_missing_ground_truth(self):
        record = make_record("reconnaissance", F, [])
        result = validate_scenario_ground_truth(
            feeder_id=F, record=record, rows=combined_rows([record], F)
        )
        self.assertEqual(result.status, STATUS_FAIL)
        self.assertIn(MISSING_GROUND_TRUTH, codes(result))

    def test_attack_rows_without_ground_truth_record_fails(self):
        record = make_record("multi_step_attack", F, [
            make_event("multi_step_attack", F, 1, ts=TS),
        ])
        record["as_dict"]["metadata"] = {
            "stages": [{"attack_id": "reconnaissance", "event_count": 1}],
            "_stage_events": [[dict(record["events"][0])]],
            "trace": "fixture",
        }
        rows = combined_rows([record], F)
        rows.append(attack_row(
            make_event("reconnaissance", F, 1, result="READ"),
            feeder_id=F, attack_id="reconnaissance", mitre_technique="T0846",
        ))
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=rows, ground_truth=make_ground_truth(F, [record]),
        )
        self.assertEqual(report.validation_status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, [c for c, _ in report.errors])

    def test_normal_count_mismatch_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows([record], F, n_normal=3),
            ground_truth=make_ground_truth(F, [record], n_normal=5),
        )
        self.assertEqual(report.validation_status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, [c for c, _ in report.errors])

    def test_manifest_event_count_mismatch_fails(self):
        record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
            make_event("reconnaissance", F, 2, result="READ"),
        ])
        manifest = {
            "scenarios": [
                {"attack_id": "reconnaissance", "feeder_id": F, "status": "success",
                 "event_count": 1, "mitre_technique": "T0846"},
            ]
        }
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows([record], F),
            ground_truth=make_ground_truth(F, [record]), manifest=manifest,
        )
        self.assertEqual(report.validation_status, STATUS_FAIL)
        self.assertIn(GROUND_TRUTH_MISMATCH, [c for c, _ in report.errors])


# --------------------------------------------------------------------------- #
# feeder-level end-to-end + report writer
# --------------------------------------------------------------------------- #
class FeederAndWriterTests(unittest.TestCase):
    def test_full_valid_dataset_passes_all_checks(self):
        recon = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, message_type="QUERY",
                       direction="SCADA_TO_RTU", src_device="SCADA_MASTER",
                       dst_device=rtu_of(F), injected="1", result="DISCOVERED"),
            make_event("reconnaissance", F, 2, result="READ",
                       original_value="7340.5", reported_value="7340.5"),
        ])
        multi = _multi_step_record(ordered=True)
        records = [recon, multi]
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows(records, F),
            ground_truth=make_ground_truth(F, records),
        )
        self.assertEqual(report.validation_status, STATUS_PASS)
        self.assertEqual(report.total_events, 9)
        self.assertEqual(report.normal_events, 3)
        self.assertEqual(report.attack_events, 6)
        self.assertEqual(report.checks["labels"], STATUS_PASS)
        self.assertEqual(report.checks["mitre"], STATUS_PASS)
        self.assertEqual(report.checks["window"], STATUS_PASS)
        self.assertEqual(len(report.scenarios), 2)
        self.assertEqual(report.errors, ())

    def test_write_validation_report_shape_and_totals(self):
        recon = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        rows = combined_rows([recon], F, n_normal=2)
        report = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=rows,
            ground_truth=make_ground_truth(F, [recon], n_normal=2),
        )
        self.assertEqual(report.validation_status, STATUS_PASS)
        out_dir = Path(tempfile.mkdtemp(prefix="gt_report_"))
        path, sha = write_validation_report([report], results_dir=out_dir)
        self.assertTrue(path.is_file())
        self.assertEqual(len(sha), 64)
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "validation_status", "generated_at", "feeders", "scenarios",
            "total_events", "attack_events", "normal_events", "checks",
            "errors", "warnings",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["validation_status"], STATUS_PASS)
        self.assertEqual(data["total_events"], len(rows))
        self.assertEqual(data["normal_events"], 2)
        self.assertEqual(data["attack_events"], 1)
        self.assertEqual(data["feeders"][0]["feeder_id"], F)

    def test_aggregate_reports_status_fail_when_any_feeder_fails(self):
        ok = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        good = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=combined_rows([ok], F),
            ground_truth=make_ground_truth(F, [ok]),
        )
        bad_record = make_record("reconnaissance", F, [
            make_event("reconnaissance", F, 1, result="READ"),
        ])
        bad_rows = combined_rows([bad_record], F)
        for row in bad_rows:
            if str(row.get(ATTACK_ID_COLUMN, "")) == "reconnaissance":
                row[MITRE_TECHNIQUE_COLUMN] = "T9999"
        bad = validate_feeder_ground_truth(
            feeder_id=F, combined_rows=bad_rows,
            ground_truth=make_ground_truth(F, [bad_record]),
        )
        self.assertEqual(good.validation_status, STATUS_PASS)
        self.assertEqual(bad.validation_status, STATUS_FAIL)
        agg = aggregate_reports([good, bad])
        self.assertEqual(agg["validation_status"], STATUS_FAIL)
        self.assertTrue(agg["errors"])
        self.assertIn("feeder_id", agg["errors"][0])

    def test_error_code_vocabulary_complete(self):
        expected = {
            "MISSING_MITRE_LABEL", "INVALID_ATTACK_LABEL", "MISSING_GROUND_TRUTH",
            "INVALID_SCENARIO_ID", "INVALID_ATTACK_ID",
            "EVENT_OUTSIDE_ATTACK_WINDOW", "INVALID_TIME_WINDOW",
            "MULTI_STEP_ORDER_ERROR", "GROUND_TRUTH_MISMATCH",
        }
        self.assertEqual(set(ERROR_CODES), expected)
        self.assertEqual(len(ERROR_CODES), len(expected))


if __name__ == "__main__":
    unittest.main()