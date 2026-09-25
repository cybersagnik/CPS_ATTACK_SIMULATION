"""Task H tests: canonical Task H schema builders + fail-closed validation.

Covers the Task H package's own canonical constants (verbatim Phase E 16
columns, label injection, deterministic key, 25-column width).  Deterministic
by construction: no OpenDSS, no CLI, no engine; pure schema behavior.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Tuple

from simulator.attack.base import AttackContext
from simulator.attack.engine import AttackEngine
from simulator.attack.results import RESULT_SUCCESS
from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for
from simulator.dataset.errors import (
    DatasetError,
    DatasetGenerationError,
    DatasetValidationError,
)
from simulator.dataset.exporter import (
    write_attack_only_csv,
    write_combined_csv,
    write_ground_truth_json,
)
from simulator.dataset.generator import FeederDataset, generate_feeder_dataset
from simulator.dataset.manifest import write_manifest
from simulator.dataset.schema import (
    ATTACK_ID_COLUMN,
    DATASET_COLUMNS,
    LABELS,
    LABEL_ATTACK,
    LABEL_COLUMN,
    LABEL_NORMAL,
    MITRE_TECHNIQUE_COLUMN,
    attack_row,
    combined_sort_key,
    normal_row,
    validate_combined,
)
from simulator.network.events import rtu_of
from simulator.power.loader import FeederLoader

ROOT = Path(__file__).resolve().parents[2]
TS = "2010-07-01T00:00:00"

NEW_SCENARIOS = (
    "false_measurement",
    "communication_disruption",
    "multi_step_attack",
)


def _opendss_ok() -> bool:
    try:
        import opendssdirect  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False

SUCCESS_SCENARIOS = (
    "reconnaissance",
    "unauthorized_command",
    "parameter_modification",
)

NORMAL = {
    "event_id": "2010-07-01T06:00:00Z/ieee37/N/00000001",
    "scenario_id": "normal_ieee37",
    "timestamp": "2010-07-01T06:00:00Z",
    "feeder_id": "ieee37",
    "src_device": "R1",
    "dst_device": "REG1",
    "protocol": "DNP3",
    "message_type": "measurement",
    "direction": "outbound",
    "sequence": "1001",
    "point_id": "V1",
    "value": "7340.5",
    "unit": "V",
    "quality": "good",
    "delivery_status": "delivered",
    "latency_ms": "12",
}

ATTACK = {
    "event_id": "2010-07-01T06:30:00Z/ieee37/reconnaissance/00000001",
    "scenario_id": "reconnaissance",
    "timestamp": "2010-07-01T06:30:00Z",
    "feeder_id": "ieee37",
    "src_device": "PLC7",
    "dst_device": "HMI",
    "protocol": "DNP3",
    "message_type": "scan",
    "direction": "inbound",
    "sequence": "2001",
    "point_id": "A0",
    "value": "0",
    "unit": "",
    "quality": "good",
    "delivery_status": "delivered",
    "latency_ms": "5",
    "injected": "true",
    "replay_of_event_id": "",
    "original_value": "",
    "reported_value": "",
    "command_id": "",
    "result": "success",
}


class TaskHTest(unittest.TestCase):
    def test_errors_hierarchy(self):
        self.assertTrue(issubclass(DatasetGenerationError, DatasetError))
        self.assertTrue(issubclass(DatasetValidationError, DatasetError))

    def test_schema_widths(self):
        self.assertEqual(len(DATASET_COLUMNS), 25)
        self.assertEqual(LABELS, (LABEL_NORMAL, LABEL_ATTACK))

    def test_normal_row_is_verbatim_16col(self):
        row = normal_row(NORMAL, feeder_id="ieee37")
        for col, value in NORMAL.items():
            self.assertEqual(row[col], value)
        self.assertEqual(row[LABEL_COLUMN], LABEL_NORMAL)

    def test_attack_row_adds_three_task_h_columns(self):
        row = attack_row(
            ATTACK,
            feeder_id="ieee37",
            attack_id="reconnaissance",
            mitre_technique="T0846",
        )
        self.assertEqual(row[LABEL_COLUMN], LABEL_ATTACK)
        self.assertEqual(row[ATTACK_ID_COLUMN], "reconnaissance")
        self.assertEqual(row[MITRE_TECHNIQUE_COLUMN], "T0846")

    def test_combined_sort_key_deterministic(self):
        n = normal_row(NORMAL, feeder_id="ieee37")
        self.assertEqual(combined_sort_key(n), combined_sort_key(n))

    def test_combined_sort_key_normal_before_attack_at_equal_ts(self):
        n = normal_row(NORMAL, feeder_id="ieee37")
        a = attack_row(
            ATTACK,
            feeder_id="ieee37",
            attack_id="reconnaissance",
            mitre_technique="T0846",
        )
        self.assertLess(combined_sort_key(n), combined_sort_key(a))

    def test_validate_combined_accepts_canonical_two_row_dataset(self):
        rows = [
            normal_row(NORMAL, feeder_id="ieee37"),
            attack_row(
                ATTACK,
                feeder_id="ieee37",
                attack_id="reconnaissance",
                mitre_technique="T0846",
            ),
        ]
        validate_combined(
            rows,
            feeder_id="ieee37",
            expected_scenario_ids=SUCCESS_SCENARIOS,
            expected_attack_ids=SUCCESS_SCENARIOS,
        )  # fail-closed: no exception => valid

    def test_validate_combined_rejects_missing_normal(self):
        rows = [
            attack_row(
                ATTACK,
                feeder_id="ieee37",
                attack_id="reconnaissance",
                mitre_technique="T0846",
            )
        ]
        with self.assertRaises(DatasetError):
            validate_combined(
                rows,
                feeder_id="ieee37",
                expected_scenario_ids=SUCCESS_SCENARIOS,
                expected_attack_ids=SUCCESS_SCENARIOS,
            )

    def test_validate_combined_rejects_invalid_label(self):
        bad = dict(NORMAL)
        bad[LABEL_COLUMN] = "NOT_REAL"
        rows = [normal_row(bad, feeder_id="ieee37")]
        with self.assertRaises(DatasetError):
            validate_combined(
                rows,
                feeder_id="ieee37",
                expected_scenario_ids=SUCCESS_SCENARIOS,
                expected_attack_ids=SUCCESS_SCENARIOS,
            )

    def test_validate_combined_rejects_out_of_order(self):
        rows = [
            attack_row(
                ATTACK,
                feeder_id="ieee37",
                attack_id="reconnaissance",
                mitre_technique="T0846",
            ),
            normal_row(NORMAL, feeder_id="ieee37"),
        ]
        with self.assertRaises(DatasetError):
            validate_combined(
                rows,
                feeder_id="ieee37",
                expected_scenario_ids=SUCCESS_SCENARIOS,
                expected_attack_ids=SUCCESS_SCENARIOS,
            )

    def test_manifest_records_scenario_summary(self):
        scenarios = [
            {
                "attack_id": "false_measurement",
                "feeder_id": "ieee37",
                "status": "success",
                "event_count": 2,
                "mitre_technique": "T0856",
                "output": "attack_ieee37_false_measurement_dataset.csv",
            },
            {
                "attack_id": "multi_step_attack",
                "feeder_id": "ieee37",
                "status": "success",
                "event_count": 278,
                "mitre_technique": "T0846",
                "output": "attack_ieee37_multi_step_attack_dataset.csv",
            },
        ]
        out_dir = Path(tempfile.mkdtemp(prefix="taskh_manifest_"))
        path, _ = write_manifest(
            [],
            results_dir=out_dir,
            feeder_id="ieee37",
            seed=42,
            record={
                "combined_sha256": "ab",
                "determinism_digest": "ab",
                "scenarios": scenarios,
            },
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["scenarios"], scenarios)


def _generate_and_export(feeder_id: str) -> Tuple[Path, FeederDataset]:
    """One deterministic generate + export per feeder (memoised for the suite)."""
    if feeder_id in _EXPORT_CACHE:
        return _EXPORT_CACHE[feeder_id]
    events_csv = ROOT / "results" / feeder_id / f"normal_{feeder_id}_network_events.csv"
    dataset = generate_feeder_dataset(
        feeder_id,
        seed=42,
        results_dir=ROOT / "results",
        events_csv=events_csv,
    )
    out_dir = Path(tempfile.mkdtemp(prefix="taskh_export_"))
    rows = dataset.combined_rows()
    write_combined_csv(rows, feeder_id=feeder_id, results_dir=out_dir, seed=42)
    for attack_id in NEW_SCENARIOS:
        write_attack_only_csv(
            rows, feeder_id=feeder_id, attack_id=attack_id,
            results_dir=out_dir, seed=42,
        )
    ground_truth = {
        "feeder_id": feeder_id,
        "scenario_id": f"combined_{feeder_id}",
        "seed": 42,
        "normal": dict(dataset.ground_truth_normal),
        "attacks": [dict(gt) for gt in dataset.ground_truth_attacks],
        "failed_scenarios": dict(dataset.failed_scenarios),
        "combined_rows": len(rows),
    }
    write_ground_truth_json(ground_truth, feeder_id=feeder_id, results_dir=out_dir)
    _EXPORT_CACHE[feeder_id] = (out_dir, dataset)
    return _EXPORT_CACHE[feeder_id]


_EXPORT_CACHE: Dict[str, Tuple[Path, FeederDataset]] = {}


@unittest.skipUnless(_opendss_ok(), "opendssdirect not available")
class TaskHExportIntegrationTests(unittest.TestCase):
    """Real Task H export of the three extension scenarios (ieee37 + ieee123).

    Runs the *existing* generator over the real feeder model and real Phase E
    CSV, then the existing exporter, and verifies the CSV / JSON / ground-truth
    content for False Measurement, Communication Disruption and Multi-Step
    Attack.  Generation is memoised per feeder so the three scenarios share one
    deterministic dataset per feeder.
    """

    FEEDERS = ("ieee37", "ieee123")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _out_dir(self, feeder_id: str) -> Path:
        return _generate_and_export(feeder_id)[0]

    def _attack_rows(self, feeder_id: str, attack_id: str):
        path = self._out_dir(feeder_id) / f"attack_{feeder_id}_{attack_id}_dataset.csv"
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _gt(self, feeder_id: str):
        with (self._out_dir(feeder_id) / f"ground_truth_{feeder_id}.json").open(
            encoding="utf-8"
        ) as fh:
            return json.load(fh)

    def _gt_attack(self, feeder_id: str, attack_id: str):
        for rec in self._gt(feeder_id)["attacks"]:
            if rec["attack_id"] == attack_id:
                return rec
        self.fail(f"attack {attack_id!r} missing from exported ground truth")

    # ------------------------------------------------------------------ #
    # False Measurement
    # ------------------------------------------------------------------ #
    def test_false_measurement_export_csv_json(self):
        for feeder_id in self.FEEDERS:
            with self.subTest(feeder=feeder_id):
                rows = self._attack_rows(feeder_id, "false_measurement")
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["label"], LABEL_ATTACK)
                    self.assertEqual(row["attack_id"], "false_measurement")
                    self.assertEqual(row[MITRE_TECHNIQUE_COLUMN], "T0856")
                    self.assertEqual(row["scenario_id"], scenario_id_for("false_measurement", feeder_id))
                    self.assertEqual(row["feeder_id"], feeder_id)
                query, report = rows
                self.assertEqual(query["message_type"], "QUERY")
                self.assertEqual(query["injected"], "1")       # forged interrogation
                self.assertEqual(query["result"], "READ")
                self.assertEqual(report["message_type"], "REPORT")
                self.assertEqual(report["injected"], "1")       # forged reporting message
                self.assertEqual(report["result"], "SPOOFED")
                # original (physical truth) vs reported (cyber truth) kept separate
                self.assertTrue(report["original_value"])
                self.assertTrue(report["reported_value"])
                self.assertNotEqual(report["original_value"], report["reported_value"])
                rec = self._gt_attack(feeder_id, "false_measurement")
                self.assertEqual(rec["mitre_technique_ids"], "T0856")
                self.assertEqual(rec["event_count"], 2)
                ev_report = rec["events"][1]
                self.assertEqual(ev_report["original_value"], report["original_value"])
                self.assertEqual(ev_report["reported_value"], report["reported_value"])
                # the falsified report must not overwrite the physical value
                self.assertNotAlmostEqual(
                    float(ev_report["reported_value"]), rec["original_value"], places=6
                )
                self.assertEqual(ev_report["point_id"], rec["target_point_id"])
                # honest physical effect: grid untouched, no fabricated change
                effect = rec["physical_effect"]
                self.assertEqual(effect["status"], "computed")
                self.assertEqual(effect["deltas"]["source_p_kw"], 0.0)
                self.assertEqual(effect["deltas"]["vpu_min"], 0.0)
                self.assertTrue((self._out_dir(feeder_id) / f"combined_{feeder_id}_dataset.csv").exists())
                self.assertTrue((self._out_dir(feeder_id) / f"ground_truth_{feeder_id}.json").exists())
                self.assertTrue((self._out_dir(feeder_id) / f"attack_{feeder_id}_false_measurement_dataset.csv").exists())

    # ------------------------------------------------------------------ #
    # Communication Disruption
    # ------------------------------------------------------------------ #
    def test_communication_disruption_export_csv_json(self):
        for feeder_id in self.FEEDERS:
            with self.subTest(feeder=feeder_id):
                rows = self._attack_rows(feeder_id, "communication_disruption")
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["attack_id"], "communication_disruption")
                    self.assertEqual(row[MITRE_TECHNIQUE_COLUMN], "T0804")
                    self.assertEqual(row["protocol"], "DNP3")
                query, report = rows
                self.assertEqual(query["message_type"], "QUERY")
                self.assertEqual(query["injected"], "1")       # adversarial block instruction
                self.assertEqual(query["result"], "BLOCK")
                self.assertEqual(query["direction"], "SCADA_TO_RTU")
                self.assertEqual(query["src_device"], "SCADA_MASTER")
                self.assertEqual(query["dst_device"], rtu_of(feeder_id))
                # genuine measurement, never delivered
                self.assertEqual(report["message_type"], "REPORT")
                self.assertEqual(report["injected"], "0")
                self.assertEqual(report["delivery_status"], "DROPPED")
                self.assertEqual(report["result"], "BLOCKED")
                self.assertEqual(report["direction"], "RTU_TO_SCADA")
                self.assertEqual(report["src_device"], rtu_of(feeder_id))
                self.assertEqual(report["dst_device"], "SCADA_MASTER")
                self.assertEqual(report["latency_ms"], "0")
                rec = self._gt_attack(feeder_id, "communication_disruption")
                self.assertEqual(rec["mitre_technique_ids"], "T0804")
                self.assertEqual(rec["events"][1]["delivery_status"], "DROPPED")
                self.assertEqual(rec["events"][1]["message_type"], "REPORT")
                # the measurement itself is taken truthfully
                self.assertEqual(rec["reported_value"], rec["original_value"])
                effect = rec["physical_effect"]
                self.assertEqual(effect["status"], "computed")
                self.assertEqual(effect["deltas"]["source_p_kw"], 0.0)
                self.assertEqual(effect["deltas"]["vpu_min"], 0.0)

    # ------------------------------------------------------------------ #
    # Multi-Step Attack
    # ------------------------------------------------------------------ #
    def test_multi_step_export_csv_json_chronology(self):
        for feeder_id in self.FEEDERS:
            with self.subTest(feeder=feeder_id):
                rows = self._attack_rows(feeder_id, "multi_step_attack")
                rec = self._gt_attack(feeder_id, "multi_step_attack")
                self.assertEqual(len(rows), rec["event_count"])
                for row in rows:
                    self.assertEqual(row["label"], LABEL_ATTACK)
                    self.assertEqual(row["attack_id"], "multi_step_attack")
                    self.assertEqual(row[MITRE_TECHNIQUE_COLUMN], "T0846")  # lead technique id
                    self.assertEqual(
                        row["scenario_id"], scenario_id_for("multi_step_attack", feeder_id)
                    )
                # chronology: non-decreasing timestamps, three ordered phase buckets
                stamps = [row["timestamp"] for row in rows]
                self.assertEqual(stamps, sorted(stamps))
                buckets: Dict[str, int] = {}
                for row in rows:
                    buckets[row["timestamp"]] = buckets.get(row["timestamp"], 0) + 1
                self.assertEqual(
                    sorted(buckets),
                    ["2010-07-01T00:00:00", "2010-07-01T00:00:01", "2010-07-01T00:00:02"],
                )
                self.assertEqual(buckets["2010-07-01T00:00:01"], 2)
                self.assertEqual(buckets["2010-07-01T00:00:02"], 2)
                # event ids unique + scenario-scoped; sequence verbatim 1..N
                ids = [row["event_id"] for row in rows]
                self.assertEqual(len(set(ids)), len(ids))
                self.assertTrue(all(i.startswith(scenario_id_for("multi_step_attack", feeder_id)) for i in ids))
                seq = sorted(int(row["sequence"]) for row in rows)
                self.assertEqual(seq, list(range(1, len(rows) + 1)))
                # multi-step metadata reconstructs the full sequence
                stages_meta = rec["as_dict"]["metadata"]
                self.assertEqual(
                    [s["attack_id"] for s in stages_meta["stages"]],
                    ["reconnaissance", "unauthorized_command", "parameter_modification"],
                )
                stage_events = stages_meta["_stage_events"]
                self.assertEqual(len(stage_events), 3)
                self.assertTrue(
                    all(isinstance(ev, dict) for stage in stage_events for ev in stage)
                )
                self.assertEqual(
                    [len(s) for s in stage_events],
                    [s["event_count"] for s in stages_meta["stages"]],
                )
                self.assertEqual(sum(len(s) for s in stage_events), rec["event_count"])
                self.assertEqual(rec["mitre_technique_ids"], "T0846;T0855;T0836")
                effect = rec["physical_effect"]
                self.assertEqual(effect["status"], "computed")
                self.assertNotEqual(effect["deltas"]["source_p_kw"], 0.0)

    # ------------------------------------------------------------------ #
    # Ground truth matches the Attack Engine result
    # ------------------------------------------------------------------ #
    def test_exported_ground_truth_matches_engine_result(self):
        for feeder_id in self.FEEDERS:
            model = FeederLoader().load(feeder_id)
            dataset = _generate_and_export(feeder_id)[1]
            self.assertNotIn(feeder_id, dataset.failed_scenarios)
            for attack_id in NEW_SCENARIOS:
                with self.subTest(feeder=feeder_id, attack=attack_id):
                    rec = self._gt_attack(feeder_id, attack_id)
                    ctx = AttackContext(
                        scenario_id=scenario_id_for(attack_id, feeder_id),
                        feeder_id=feeder_id,
                        model=model,
                        event_path=ROOT / "results" / feeder_id
                        / f"normal_{feeder_id}_network_events.csv",
                        timestamp=TS,
                        config={},
                        random_seed=42,
                    )
                    result = AttackEngine(seed=42).run(
                        REGISTERED_SCENARIOS[attack_id], ctx
                    )
                    self.assertEqual(result.result, RESULT_SUCCESS)
                    self.assertEqual(rec["events"], result.event_rows())
                    self.assertEqual(rec["event_count"], len(result.event_rows()))
                    self.assertEqual(
                        rec["mitre_technique_ids"],
                        result.ground_truth()["mitre_technique_ids"],
                    )
                    self.assertEqual(rec["original_value"], result.original_value)
                    self.assertEqual(rec["reported_value"], result.reported_value)
                    self.assertEqual(rec["physical_effect"], result.physical_effect)
                    if attack_id == "multi_step_attack":
                        self.assertEqual(
                            rec["as_dict"]["metadata"]["stages"], result.metadata["stages"]
                        )
                        stage_events = [
                            [e.as_dict() for e in stage]
                            for stage in result.metadata["_stage_events"]
                        ]
                        self.assertEqual(
                            rec["as_dict"]["metadata"]["_stage_events"], stage_events
                        )


if __name__ == "__main__":
    unittest.main()