"""Task H tests: canonical Task H schema builders + fail-closed validation.

Covers the Task H package's own canonical constants (verbatim Phase E 16
columns, label injection, deterministic key, 25-column width).  Deterministic
by construction: no OpenDSS, no CLI, no engine; pure schema behavior.
"""

from __future__ import annotations

import unittest

from simulator.dataset.errors import (
    DatasetError,
    DatasetGenerationError,
    DatasetValidationError,
)
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


if __name__ == "__main__":
    unittest.main()