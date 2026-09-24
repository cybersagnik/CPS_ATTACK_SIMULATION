#!/usr/bin/env python3
"""Unit tests for simulator.network (Phase E normal network events).

Pure -- no OpenDSS, no xlrd, no feeder library required.  All fixtures are small
synthetic telemetry CSVs that mirror the physical schemas written by
``simulator.power.normal_scenario``.  Run:

    python3 -m unittest simulator.network.test_network_events -v
"""

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from simulator.network.events import (
    DEVICE_SCADA_MASTER,
    DIRECTION_RTU_TO_SCADA,
    DIRECTION_SCADA_TO_RTU,
    MESSAGE_TYPE_POLL,
    MESSAGE_TYPE_TELEMETRY,
    NETWORK_EVENT_COLUMNS,
    PROTOCOL_DNP3,
    NetworkEvent,
    NetworkEventError,
    rtu_of,
)
from simulator.network.normal import (
    NormalNetworkGenerator,
    NetworkEventRecorder,
    read_network_events,
)

FEEDER = "ieee37"
TS1 = "2010-07-01T00:00:00"
TS2 = "2010-07-01T00:30:00"

BUS_HEADER = ["timestamp", "feeder_id", "bus", "vpu_AB", "vpu_BC", "vpu_CA"]
CURRENT_HEADER = [
    "timestamp", "feeder_id", "element", "element_type", "bus", "phase", "i_amps",
]
SUMMARY_HEADER = [
    "timestamp", "feeder_id", "source_p_kw", "source_q_kvar", "converged",
    "n_buses", "n_deenergized", "vpu_min", "vpu_max",
]

BUS_ROWS = [
    # second-half of each file only, exercised by both timestamps.
    [TS1, FEEDER, "701", "0.91681953662693", "0.894226925722235", "0.9244473688514535"],
    [TS1, FEEDER, "702", "0.90640794698", "", ""],         # single-phase: only AB
    [TS2, FEEDER, "701", "0.9305422082196793", "0.9691681068078747", "0.9531600252756911"],
    [TS2, FEEDER, "702", "0.9000000000", "", ""],
    [TS2, FEEDER, "775", "", "", ""],                      # de-energised stub: no rows
]

CURRENT_ROWS = [
    [TS1, FEEDER, "Vsource.source", "source", "syssource", "A", "7.646"],
    [TS1, FEEDER, "Vsource.source", "source", "syssource", "B", "9.384"],
    [TS1, FEEDER, "Line.L_701_702", "line", "701", "A", "12.5"],
    [TS2, FEEDER, "Vsource.source", "source", "syssource", "A", "8.001"],
    [TS2, FEEDER, "Line.L_701_702", "line", "701", "A", "13.25"],
]

SUMMARY_ROWS = [
    [TS1, FEEDER, "2752.68", "1697.36", "1", "36", "0", "0.8327", "0.9385"],
    [TS2, FEEDER, "3505.98", "1593.19", "1", "124", "0", "0.8823", "0.9839"],
]

MANIFEST = {
    "scenario": "normal",
    "feeder_id": FEEDER,
    "window": {"start": TS1, "days": 1, "steps": 2},
    "telemetry": {
        "bus_csv": "normal_ieee37_bus_telemetry.csv",
        "summary_csv": "normal_ieee37_summary.csv",
        "current_csv": "normal_ieee37_current_telemetry.csv",
    },
}


def _write_csv(path: Path, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


class NetworkFixture(unittest.TestCase):
    """Bootstrap a synthetic results dir with manifest + telemetry CSVs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        with open(self.dir / "normal_ieee37_manifest.json", "w", encoding="utf-8") as fh:
            json.dump(MANIFEST, fh)
        _write_csv(self.dir / "normal_ieee37_bus_telemetry.csv",
                   [BUS_HEADER, *BUS_ROWS])
        _write_csv(self.dir / "normal_ieee37_current_telemetry.csv",
                   [CURRENT_HEADER, *CURRENT_ROWS])
        _write_csv(self.dir / "normal_ieee37_summary.csv",
                   [SUMMARY_HEADER, *SUMMARY_ROWS])

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        gen = NormalNetworkGenerator(self.dir, FEEDER)
        return gen

    def _generate_rows(self):
        result = NormalNetworkGenerator(self.dir, FEEDER).generate()
        return read_network_events(result.output_path)


class NetworkEventModelTests(unittest.TestCase):
    def test_construction_defaults(self):
        ev = NetworkEvent(
            scenario_id="normal_ieee37", timestamp=TS1, feeder_id=FEEDER,
            src_device=DEVICE_SCADA_MASTER, dst_device=rtu_of(FEEDER),
            message_type=MESSAGE_TYPE_POLL, direction=DIRECTION_SCADA_TO_RTU,
        )
        ev.validate()
        self.assertEqual(ev.protocol, PROTOCOL_DNP3)
        self.assertEqual(ev.quality, "GOOD")
        self.assertEqual(ev.delivery_status, "DELIVERED")
        self.assertEqual(ev.latency_ms, 0)

    def test_field_validation_rejects_bad_values(self):
        base = dict(
            scenario_id="normal_ieee37", timestamp=TS1, feeder_id=FEEDER,
            src_device=rtu_of(FEEDER), dst_device=DEVICE_SCADA_MASTER,
            message_type=MESSAGE_TYPE_TELEMETRY, direction=DIRECTION_RTU_TO_SCADA,
            point_id="BUS_701_VPU_AB", value=0.91681953662693, unit="pu",
        )
        for kwargs in [
            {"message_type": "OPEN"},                       # unknown type
            {"message_type": MESSAGE_TYPE_POLL},            # telemetry-shaped as POLL
            {"direction": DIRECTION_SCADA_TO_RTU},          # telemetry direction flipped
            {"quality": "BAD"},                             # non-GOOD in normal phase
            {"delivery_status": "NOT_DELIVERED"},
            {"latency_ms": 42},
            {"value": None},                                # telemetry needs a value
            {"value": float("nan")},
            {"point_id": ""},                               # telemetry requires point
            {"protocol": "MODBUS"},
            {"src_device": "FEEDER_37"},                    # wrong source
            {"dst_device": "ANOTHER_MASTER"},               # wrong destination
        ]:
            with self.subTest(result=kwargs):
                merged = dict(base)
                merged.update(kwargs)
                with self.assertRaises(NetworkEventError):
                    NetworkEvent(**merged).validate()

    def test_poll_shape_is_enforced(self):
        bad = NetworkEvent(
            scenario_id="normal_ieee37", timestamp=TS1, feeder_id=FEEDER,
            src_device=DEVICE_SCADA_MASTER, dst_device=rtu_of(FEEDER),
            message_type=MESSAGE_TYPE_POLL, direction=DIRECTION_SCADA_TO_RTU,
            point_id="BUS_701_VPU_AB", value=1.0, unit="pu",
        )
        with self.assertRaises(NetworkEventError):
            bad.validate()


class NormalGeneratorTests(NetworkFixture):
    def test_events_are_generated_for_each_timestamp(self):
        rows = self._generate_rows()
        timestamps = sorted({r["timestamp"] for r in rows})
        self.assertEqual(timestamps, [TS1, TS2])

    def test_poll_generation_ordering_and_shape(self):
        rows = self._generate_rows()
        for ts in (TS1, TS2):
            ts_rows = [r for r in rows if r["timestamp"] == ts]
            poll = ts_rows[0]
            self.assertEqual(poll["message_type"], MESSAGE_TYPE_POLL)
            self.assertEqual(poll["src_device"], DEVICE_SCADA_MASTER)
            self.assertEqual(poll["dst_device"], rtu_of(FEEDER))
            self.assertEqual(poll["direction"], DIRECTION_SCADA_TO_RTU)
            self.assertEqual(poll["point_id"], "")
            self.assertEqual(poll["value"], "")
            for later in ts_rows[1:]:
                self.assertEqual(later["src_device"], rtu_of(FEEDER))
                self.assertEqual(later["dst_device"], DEVICE_SCADA_MASTER)
                self.assertEqual(later["direction"], DIRECTION_RTU_TO_SCADA)

    def test_bus_voltage_mapping(self):
        rows = self._generate_rows()
        points = {
            (r["point_id"], r["timestamp"]): r
            for r in rows if r["point_id"].startswith("BUS_")
        }
        self.assertEqual(points[("BUS_701_VPU_AB", TS1)]["value"], "0.91681953662693")
        self.assertEqual(points[("BUS_701_VPU_AB", TS1)]["unit"], "pu")
        self.assertEqual(points[("BUS_701_VPU_CA", TS1)]["value"], "0.9244473688514535")
        self.assertEqual(points[("BUS_701_VPU_AB", TS2)]["value"], "0.9305422082196793")
        # 702 is single-phase: only VPU_AB emitted, BC/CA absent
        self.assertIn(("BUS_702_VPU_AB", TS1), points)
        self.assertNotIn(("BUS_702_VPU_BC", TS1), points)
        self.assertNotIn(("BUS_702_VPU_CA", TS1), points)
        # 775 has no vpu at all
        self.assertNotIn(("BUS_775_VPU_AB", TS2), points)

    def test_current_mapping(self):
        rows = self._generate_rows()
        points = {
            (r["point_id"], r["timestamp"]): r
            for r in rows if r["point_id"].startswith("CURRENT_")
        }
        self.assertEqual(points[("CURRENT_source_Vsource_source_A", TS1)]["value"], "7.646")
        self.assertEqual(points[("CURRENT_source_Vsource_source_A", TS1)]["unit"], "A")
        self.assertEqual(points[("CURRENT_source_Vsource_source_B", TS1)]["value"], "9.384")
        self.assertEqual(points[("CURRENT_line_Line_L_701_702_A", TS1)]["value"], "12.5")
        self.assertEqual(points[("CURRENT_source_Vsource_source_A", TS2)]["value"], "8.001")

    def test_summary_mapping(self):
        rows = self._generate_rows()
        points = {
            (r["point_id"], r["timestamp"]): r
            for r in rows if r["point_id"].startswith("FEEDER_")
        }
        self.assertEqual(points[("FEEDER_SOURCE_P", TS1)]["value"], "2752.68")
        self.assertEqual(points[("FEEDER_SOURCE_P", TS1)]["unit"], "kW")
        self.assertEqual(points[("FEEDER_SOURCE_Q", TS1)]["value"], "1697.36")
        self.assertEqual(points[("FEEDER_VPU_MIN", TS1)]["value"], "0.8327")
        self.assertEqual(points[("FEEDER_VPU_MAX", TS1)]["value"], "0.9385")
        self.assertEqual(points[("FEEDER_CONVERGED", TS1)]["value"], "1.0")
        self.assertEqual(points[("FEEDER_SOURCE_P", TS2)]["value"], "3505.98")

    def test_values_equal_physical_telemetry(self):
        rows = self._generate_rows()
        # 2 POLL + 8 bus + 5 current + 10 summary telemetry points
        self.assertEqual(len(rows), 25)

    def test_timestamp_preservation(self):
        rows = self._generate_rows()
        for row in rows:
            self.assertIn(row["timestamp"], (TS1, TS2))

    def test_event_ids_and_sequence_are_deterministic_and_contiguous(self):
        result = self.build().generate()
        records = read_network_events(result.output_path)
        sequences = [int(r["sequence"]) for r in records]
        self.assertEqual(sequences, list(range(1, len(records) + 1)))
        ids = [r["event_id"] for r in records]
        self.assertEqual(len(set(ids)), len(ids))

    def test_repeated_generation_is_byte_identical(self):
        gen = self.build()
        p1 = self.dir / "n1.csv"
        p2 = self.dir / "n2.csv"
        gen.generate(p1)
        gen.generate(p2)
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())


class NetworkEventRecorderTests(unittest.TestCase):
    def test_csv_schema_is_the_canonical_column_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "events.csv"
            recorder = NetworkEventRecorder(out)
            recorder.write([
                NetworkEvent(
                    scenario_id="s", timestamp=TS1, feeder_id=FEEDER,
                    src_device=rtu_of(FEEDER), dst_device=DEVICE_SCADA_MASTER,
                    message_type=MESSAGE_TYPE_TELEMETRY, direction=DIRECTION_RTU_TO_SCADA,
                    point_id="BUS_701_VPU_AB", value=0.9, unit="pu",
                )
            ])
            recorder.close()
            with open(out, encoding="utf-8") as fh:
                header = next(csv.reader(fh))
            self.assertEqual(header, list(NETWORK_EVENT_COLUMNS))

    def test_recorder_rejects_out_of_order_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = NetworkEventRecorder(Path(tmp) / "events.csv")
            with self.assertRaises(NetworkEventError):
                recorder.write([self._event(TS1), self._event(TS2)])  # ok
                recorder.write([self._event(TS1)])  # regresses
            recorder.close()

    @staticmethod
    def _event(ts):
        return NetworkEvent(
            scenario_id="s", timestamp=ts, feeder_id=FEEDER,
            src_device=rtu_of(FEEDER), dst_device=DEVICE_SCADA_MASTER,
            message_type=MESSAGE_TYPE_TELEMETRY, direction=DIRECTION_RTU_TO_SCADA,
            point_id="BUS_701_VPU_AB", value=0.9, unit="pu",
        )


class MissingSourceTests(NetworkFixture):
    def test_missing_manifest_raises(self):
        (self.dir / "normal_ieee37_manifest.json").unlink()
        with self.assertRaises(NetworkEventError):
            self.build()

    def test_missing_telemetry_file_raises(self):
        (self.dir / "normal_ieee37_current_telemetry.csv").unlink()
        with self.assertRaises(NetworkEventError):
            self.build()

    def test_malformed_value_raises(self):
        _write_csv(self.dir / "normal_ieee37_bus_telemetry.csv", [
            BUS_HEADER,
            [TS1, FEEDER, "701", "not-a-number", "0.89", "0.92"],
        ])
        with self.assertRaises(NetworkEventError):
            self.build().generate()


if __name__ == "__main__":
    unittest.main()