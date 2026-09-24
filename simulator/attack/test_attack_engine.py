#!/usr/bin/env python3
"""Tests for the attack engine and MITRE-ICS scenarios (Task F / G).

Mix of pure unit tests (catalog, inventory, selection, events, engine wiring,
feeder-independence) and real-data tests against the actual ``ieee37`` /
``ieee123`` models and their ``normal_*_network_events.csv``.  The physical
(OpenDSS) tests skip gracefully when the solver is not importable.

Run:

    python3 -m unittest simulator.attack.test_attack_engine -v
"""

import unittest
from pathlib import Path
from typing import Dict, List, Tuple

from simulator.attack.base import Attack, AttackContext, AttackActionResult
from simulator.attack.engine import AttackEngine
from simulator.attack.events import (
    ATTACK_EVENT_COLUMNS,
    AttackEvent,
    validate_attack_event,
)
from simulator.attack.mitre import MITREMapping, mitre_for
from simulator.attack.results import AttackResult, RESULT_FAILED, RESULT_SUCCESS
from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for
from simulator.attack.targets import (
    Inventory,
    MeasurementPoint,
    ControlPoint,
    SelectedTarget,
    SelectionRequirement,
    TargetSelector,
    build_inventory,
    build_point_inventory,
    control_point_id,
    point_kind,
    validate_target,
)
from simulator.power.loader import FeederLoader

ROOT = Path(__file__).resolve().parents[2]
TS = "2010-07-01T00:00:00"


def _opendss_ok() -> bool:
    try:
        import opendssdirect  # noqa: F401
        return True
    except Exception:
        return False


def _events_csv(feeder_id: str) -> Path:
    return ROOT / "results" / feeder_id / f"normal_{feeder_id}_network_events.csv"


class MITRECatalogTests(unittest.TestCase):
    def test_technique_ids_are_the_verified_real_ones(self):
        for technique_id in ("T0846", "T0855", "T0836"):
            mapping = mitre_for(technique_id)
            self.assertIsInstance(mapping, MITREMapping)
            self.assertEqual(mapping.technique_id, technique_id)
            self.assertTrue(mapping.technique_name)
            self.assertTrue(mapping.rationale)

    def test_t0846_is_remote_system_discovery(self):
        m = mitre_for("T0846")
        self.assertEqual(m.technique_name, "Remote System Discovery")
        self.assertEqual(m.tactic, "Discovery")

    def test_t0855_and_t0836_impair_process_control(self):
        self.assertEqual(mitre_for("T0855").tactic, "Impair Process Control")
        self.assertEqual(mitre_for("T0836").tactic, "Impair Process Control")

    def test_unknown_technique_raises(self):
        with self.assertRaises(KeyError):
            mitre_for("T9999")


class PointInventoryTests(unittest.TestCase):
    def test_point_kind_classification_is_feeder_agnostic(self):
        self.assertEqual(point_kind("BUS_701_VPU_AB"), "voltage")
        self.assertEqual(point_kind("CURRENT_load_001_A"), "current")
        self.assertEqual(point_kind("FEEDER_SOURCE_P"), "power")
        self.assertEqual(point_kind("FEEDER_VPU_MIN"), "voltage")
        self.assertEqual(point_kind("FEEDER_CONVERGED"), "boolean")
        self.assertEqual(point_kind("CTRL_load_load_007_kw_multiplier"), "other")

    def test_control_point_id_is_deterministic(self):
        self.assertEqual(
            control_point_id("load", "load/7", "kw_multiplier"),
            "CTRL_load_load_7_kw_multiplier",
        )

    def test_build_point_inventory_dedupes_by_point_id(self):
        rows = [
            {"point_id": "BUS_701_VPU_AB", "message_type": "TELEMETRY",
             "unit": "pu", "value": "1.01", "timestamp": TS, "event_id": "ev1"},
            {"point_id": "BUS_701_VPU_AB", "message_type": "TELEMETRY",
             "unit": "pu", "value": "1.02", "timestamp": TS, "event_id": "ev2"},
            {"point_id": "CURRENT_load_001_A", "message_type": "TELEMETRY",
             "unit": "amps", "value": "12.5", "timestamp": TS, "event_id": "ev3"},
            {"point_id": "CURRENT_load_001_A", "message_type": "POLL",
             "unit": "amps", "value": "", "timestamp": TS, "event_id": "ev4"},
        ]
        inv = build_point_inventory(rows, "any_feeder", reference_timestamp=TS)
        self.assertEqual(len(inv.measurement_points), 2)
        self.assertEqual(inv.measurement("BUS_701_VPU_AB").count, 2)
        self.assertAlmostEqual(inv.measurement("BUS_701_VPU_AB").reference_value, 1.02)
        self.assertEqual(inv.measurement("CURRENT_load_001_A").count, 1)

    def test_target_selector_is_deterministic_and_filters_by_kind(self):
        inv = Inventory.empty("f")
        for pid in ("BUS_605_VPU_AB", "BUS_602_VPU_AB", "CURRENT_load_002_B"):
            inv.measurement_points.append(
                MeasurementPoint(pid, point_kind(pid), "pu", "f", 1, 1.0, TS, "e1")
            )
        sel = TargetSelector(inv, seed=42).select(
            SelectionRequirement(kind="measurement", sub_kinds=("voltage",)))
        self.assertEqual(sel.status, "SELECTED")
        self.assertEqual(len(sel.candidates), 2)
        self.assertEqual(sel.target.point_id, "BUS_602_VPU_AB")
        again = TargetSelector(inv, seed=42).select(
            SelectionRequirement(kind="measurement", sub_kinds=("voltage",)))
        self.assertEqual(again.target.point_id, sel.target.point_id)

    def test_no_target_fails_cleanly(self):
        sel = TargetSelector(Inventory.empty("f"), seed=42).select(
            SelectionRequirement(kind="measurement", sub_kinds=("voltage",)))
        self.assertEqual(sel.status, "FAILED")
        self.assertIsNone(sel.target)
        self.assertTrue(sel.failure_reason)

    def test_validate_target_checks_feeder_and_existence(self):
        inv = Inventory.empty("ieee37")
        inv.measurement_points.append(
            MeasurementPoint("BUS_701_VPU_AB", "voltage", "pu", "ieee37", 1, 1.01, TS, "e1")
        )
        ok, _ = validate_target(inv, inv.measurement("BUS_701_VPU_AB").selected(), "ieee37")
        self.assertTrue(ok)
        foreign = MeasurementPoint("BUS_1_VPU_AB", "voltage", "pu", "other", 1, 1.0, TS, "e1")
        ok, reason = validate_target(inv, foreign.selected(), "ieee37")
        self.assertFalse(ok)
        self.assertIn("belongs to feeder 'other'", reason)
        missing = SelectedTarget("BUS_999_VPU_AB", "measurement", sub_kind="voltage",
                                 feeder_id="ieee37", value=1.0)
        ok, reason = validate_target(inv, missing, "ieee37")
        self.assertFalse(ok)
        self.assertIn("does not exist", reason)


class AttackEventModelTests(unittest.TestCase):
    def test_attack_event_columns_extend_normal_schema(self):
        expected = (
            "event_id", "scenario_id", "timestamp", "feeder_id", "src_device",
            "dst_device", "protocol", "message_type", "direction", "sequence",
            "point_id", "value", "unit", "quality", "delivery_status", "latency_ms",
            "injected", "replay_of_event_id", "original_value", "reported_value",
            "command_id", "result",
        )
        self.assertEqual(ATTACK_EVENT_COLUMNS, expected)
        self.assertEqual(len(ATTACK_EVENT_COLUMNS), len(set(ATTACK_EVENT_COLUMNS)))

    def test_generated_event_round_trips_through_as_dict(self):
        event = AttackEvent(
            scenario_id="unauthorized_command_ieee123", timestamp=TS, feeder_id="ieee123",
            src_device="SCADA_MASTER", dst_device="RTU_ieee123",
            message_type="COMMAND", direction="SCADA_TO_RTU",
            point_id="CTRL_switch_switch_010_closed", value=1.0,
            injected=True, original_value=False, reported_value=True,
            command_id="CMD-X", result="APPLIED",
        ).with_identity("unauthorized_command_ieee123-atk001", 1)
        validate_attack_event(event)
        row = event.as_dict()
        self.assertEqual(list(row.keys()), list(ATTACK_EVENT_COLUMNS))
        self.assertEqual(row["event_id"], "unauthorized_command_ieee123-atk001")

    def test_attack_event_validation_is_strict_for_commands(self):
        bad = AttackEvent(
            scenario_id="s", timestamp=TS, feeder_id="f",
            src_device="SCADA_MASTER", dst_device="RTU_f",
            message_type="COMMAND", direction="SCADA_TO_RTU",
            point_id="CTRL_a_b_c", value=1.0, command_id="",
        )
        with self.assertRaises(ValueError):
            validate_attack_event(bad)

    def test_sort_key_orders_point_ids(self):
        def ev(pid):
            return AttackEvent(scenario_id="s", timestamp=TS, feeder_id="f",
                               src_device="SCADA_MASTER", dst_device="RTU_f",
                               message_type="REPORT", point_id=pid, value=1.0)
        self.assertTrue(ev("BUS_A").sort_key() < ev("BUS_B").sort_key())


class _ScriptedAttack(Attack):
    """A trivial attack used to exercise the engine wiring (no real scenario)."""

    def __init__(self, require_model: bool = False) -> None:
        self.attack_id = "scripted"
        self.name = "Scripted"
        self.description = "test"
        self.category = "test"
        self.mitre = (mitre_for("T0846"),)
        self._require_model = require_model

    def select_targets(self, context: AttackContext):
        return TargetSelector(context.inventory, seed=context.random_seed).select(
            SelectionRequirement(kind="measurement", supported_only=True))

    def validate_preconditions(self, context: AttackContext) -> List[str]:
        if self._require_model and context.model is None:
            return ["model required"]
        return []

    def execute(self, context: AttackContext, selection):
        t = selection.target
        return AttackActionResult(status="executed", target=t,
                                  original_value=t.value, injected_value=t.value,
                                  reported_value=t.value, command_id="CMD-TEST")


class AttackEngineTests(unittest.TestCase):
    def setUp(self):
        rows = [
            {"point_id": "BUS_701_VPU_AB", "message_type": "TELEMETRY",
             "unit": "pu", "value": "1.01", "timestamp": TS, "event_id": "n1"},
        ]
        self.inventory = build_point_inventory(rows, "ieee37", reference_timestamp=TS)
        self.ctx = AttackContext(
            scenario_id="scripted_ieee37", feeder_id="ieee37",
            inventory=self.inventory, timestamp=TS,
        )
        self.engine = AttackEngine(seed=42)

    def test_success_result_is_structured_and_has_attack_identity(self):
        result = self.engine.run(_ScriptedAttack(), self.ctx)
        self.assertEqual(result.result, RESULT_SUCCESS)
        self.assertEqual(result.scenario_id, "scripted_ieee37")
        self.assertEqual(len(result.events), 2)
        for event in result.events:
            validate_attack_event(event)
            self.assertTrue(event.event_id.startswith("scripted_ieee37-atk"))
        self.assertIn("mitre_technique_ids", result.ground_truth())

    def test_unmet_precondition_fails_cleanly(self):
        result = self.engine.run(_ScriptedAttack(require_model=True), self.ctx)
        self.assertEqual(result.result, RESULT_FAILED)
        self.assertEqual(result.status, "precondition_failed")
        self.assertIn("model required", result.failure_reason)
        self.assertEqual(len(result.events), 0)

    def test_no_target_fails_cleanly(self):
        empty_ctx = AttackContext(
            scenario_id="scripted_x", feeder_id="x",
            inventory=Inventory.empty("x"), timestamp=TS,
        )
        result = self.engine.run(_ScriptedAttack(), empty_ctx)
        self.assertEqual(result.result, RESULT_FAILED)
        self.assertEqual(result.status, "no_compatible_target")
        self.assertTrue(result.failure_reason)
        self.assertEqual(result.selection.status, "FAILED")


class FutureFeederIndependenceTests(unittest.TestCase):
    """The engine must work on a feeder it has never seen (e.g. 'future_feeder')."""

    class _FakeModel:
        def controllable_parameters(self):
            return iter(())

        def supported_device_types(self):
            return {"load"}

        @property
        def loads(self):
            return []

    def _ctx(self, feeder_id: str, inventory: Inventory) -> AttackContext:
        return AttackContext(
            scenario_id=f"reconnaissance_{feeder_id}", feeder_id=feeder_id,
            model=self._FakeModel(), inventory=inventory, timestamp=TS,
        )

    def test_reconnaissance_selects_unknown_feeder_rtu(self):
        inventory = Inventory.empty("future_feeder")
        inventory.measurement_points.append(
            MeasurementPoint("BUS_900_VPU_AB", "voltage", "pu", "future_feeder", 1, 1.0, TS, "e1"))
        result = AttackEngine(seed=42).run(
            REGISTERED_SCENARIOS["reconnaissance"], self._ctx("future_feeder", inventory))
        self.assertEqual(result.result, RESULT_SUCCESS)
        self.assertEqual(result.target.point_id, "RTU_future_feeder")
        self.assertIn("RTU_future_feeder",
                      {e.src_device for e in result.events} | {e.dst_device for e in result.events})
        discovered = result.metadata["discovered_assets"]
        self.assertEqual(discovered["measurement_point_count"], 1)

    def test_unauthorized_command_targets_unknown_feeder_control_point(self):
        inventory = Inventory.empty("future_feeder")
        inventory.control_points.append(
            ControlPoint(
                point_id=control_point_id("load", "load/099", "enabled"),
                concept="load", asset_uid="load/099", asset_source_id="FUTURE-1",
                parameter="enabled", param_kind="bool", unit="",
                baseline_value=True, bus_refs=("900",), physically_supported=True,
            )
        )
        ctx = AttackContext(
            scenario_id="unauthorized_command_future_feeder", feeder_id="future_feeder",
            model=self._FakeModel(), inventory=inventory, timestamp=TS,
        )
        result = AttackEngine(seed=42).run(REGISTERED_SCENARIOS["unauthorized_command"], ctx)
        self.assertEqual(result.result, RESULT_SUCCESS)
        self.assertEqual(result.target.point_id, "CTRL_load_load_099_enabled")
        self.assertEqual(result.target.asset_source_id, "FUTURE-1")
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.selection.status, "SELECTED")


class _RealModelMixin:
    @classmethod
    def setUpClass(cls):
        cls.models = {fid: FeederLoader().load(fid) for fid in ("ieee37", "ieee123")}


class InventoryAgainstRealModelsTests(_RealModelMixin, unittest.TestCase):
    def test_control_inventory_counts_are_stable(self):
        for fid, expected in (("ieee37", 79), ("ieee123", 290)):
            inv = build_inventory(feeder_id=fid, model=self.models[fid], reference_timestamp=TS)
            self.assertEqual(len(inv.control_points), expected, fid)
            concepts = {p.concept for p in inv.control_points}
            self.assertLessEqual(concepts,
                                 {"regulator", "switch", "capacitor", "load", "generator"})
            self.assertTrue(any(p.physically_supported for p in inv.control_points))

    def test_measurement_inventory_from_real_network_events(self):
        for fid, expected in (("ieee37", 221), ("ieee123", 465)):
            inv = build_inventory(feeder_id=fid, events_csv=_events_csv(fid),
                                  reference_timestamp=TS)
            self.assertEqual(inv.activity["measurement_point_count"], expected, fid)
            for point in inv.measurement_points:
                self.assertIn(point.kind, ("voltage", "current", "power", "boolean"))
                self.assertEqual(point.feeder_id, fid)

    def test_switch_capability_varies_with_feeder(self):
        for fid in ("ieee37", "ieee123"):
            inv = build_inventory(feeder_id=fid, model=self.models[fid],
                                  events_csv=_events_csv(fid), reference_timestamp=TS)
            switch_req = SelectionRequirement(kind="control", concepts=("switch",),
                                              parameters=("closed",), supported_only=True)
            switch_sel = TargetSelector(inv, seed=42).select(switch_req)
            if fid == "ieee37":
                self.assertEqual(switch_sel.status, "FAILED")  # no switches modelled
            else:
                self.assertEqual(switch_sel.status, "SELECTED")


@unittest.skipUnless(_opendss_ok(), "opendssdirect not available")
class ScenarioRealDataTests(_RealModelMixin, unittest.TestCase):
    """End-to-end scenario execution on the real models + real event data."""

    def _run(self, feeder_id: str, attack: Attack) -> AttackResult:
        ctx = AttackContext(
            scenario_id=scenario_id_for(attack.attack_id, feeder_id),
            feeder_id=feeder_id,
            model=self.models[feeder_id],
            event_path=_events_csv(feeder_id),
            timestamp=TS,
            config={"modify_delta": 0.35},
        )
        return AttackEngine(seed=42).run(attack, ctx)

    def _assert_valid_events(self, result: AttackResult):
        self.assertGreater(len(result.events), 0)
        seen = []
        for i, event in enumerate(result.events, start=1):
            validate_attack_event(event)
            self.assertEqual(event.event_id, f"{result.scenario_id}-atk{i:03d}")
            self.assertEqual(event.scenario_id, result.scenario_id)
            self.assertEqual(event.feeder_id, result.feeder_id)
            self.assertEqual(event.protocol, "DNP3")
            self.assertEqual(list(event.as_dict().keys()), list(ATTACK_EVENT_COLUMNS))
            seen.append(event.event_id)
        self.assertEqual(len(seen), len(set(seen)))

    def test_mitre_mappings_of_scenario_implementations(self):
        self.assertEqual(REGISTERED_SCENARIOS["reconnaissance"].mitre[0].technique_id, "T0846")
        self.assertEqual(REGISTERED_SCENARIOS["unauthorized_command"].mitre[0].technique_id, "T0855")
        self.assertEqual(REGISTERED_SCENARIOS["parameter_modification"].mitre[0].technique_id, "T0836")

    def test_all_three_scenarios_execute_on_both_real_feeder_models(self):
        for feeder_id in ("ieee37", "ieee123"):
            for attack in REGISTERED_SCENARIOS.values():
                with self.subTest(feeder=feeder_id, attack=attack.attack_id):
                    result = self._run(feeder_id, attack)
                    self.assertEqual(result.result, RESULT_SUCCESS, result.failure_reason)
                    if attack.attack_id != "reconnaissance":
                        self.assertEqual(result.physical_effect["status"], "computed")
                    self._assert_valid_events(result)

    def test_reconnaissance_report_row_count_covers_discovered_surface(self):
        for feeder_id in ("ieee37", "ieee123"):
            result = self._run(feeder_id, REGISTERED_SCENARIOS["reconnaissance"])
            ground = result.metadata["discovered_assets"]
            measurement = ground["measurement_point_count"]
            self.assertGreater(measurement, 0)
            self.assertIn("endpoints", ground)
            self.assertIn("RTU_" + feeder_id, ground["endpoints"])
            inventory = build_inventory(feeder_id=feeder_id, model=self.models[feeder_id],
                                        events_csv=_events_csv(feeder_id),
                                        reference_timestamp=TS)
            numeric_control = sum(
                1 for p in inventory.control_points
                if p.param_kind in ("float", "int") and p.baseline_value is not None
            )
            measurement_reported = sum(
                1 for p in inventory.measurement_points if p.reference_value is not None
            )
            query_count = sum(1 for e in result.events if e.message_type == "QUERY")
            report_count = sum(1 for e in result.events if e.message_type == "REPORT")
            self.assertEqual(query_count, 2)
            self.assertEqual(report_count, measurement_reported + numeric_control)
            self.assertEqual(len(result.events), 2 + report_count)
            # no report may be emitted for a *boolean* control parameter
            bool_reported = [
                e for e in result.events
                if e.point_id.startswith("CTRL_")
                and e.point_id in {p.point_id for p in inventory.control_points
                                   if p.param_kind == "bool"}
            ]
            self.assertEqual(bool_reported, [])

    def test_unauthorized_command_prefers_switch_then_load(self):
        r37 = self._run("ieee37", REGISTERED_SCENARIOS["unauthorized_command"])
        self.assertEqual(r37.target.sub_kind, "load")
        self.assertEqual(r37.target.parameter, "enabled")
        r123 = self._run("ieee123", REGISTERED_SCENARIOS["unauthorized_command"])
        self.assertEqual(r123.target.sub_kind, "switch")
        self.assertEqual(r123.target.parameter, "closed")
        for result in (r37, r123):
            self.assertEqual(
                result.command_id,
                f"CMD-UNAUTHORIZED_COMMAND-"
                f"{result.target.asset_uid.replace('/', '_')}-{result.target.parameter}",
            )
            commands = [e for e in result.events if e.message_type == "COMMAND"]
            reports = [e for e in result.events if e.message_type == "REPORT"]
            self.assertEqual(len(commands), 1)
            self.assertEqual(len(reports), 1)
            self.assertTrue(commands[0].injected)        # forged command message
            self.assertFalse(reports[0].injected)        # truthful device echo
            # pre-command device state is preserved as original value everywhere
            self.assertEqual(commands[0].original_value, reports[0].original_value)
            self.assertEqual(commands[0].original_value, result.original_value)

    def test_parameter_modification_preserves_original_value(self):
        for feeder_id in ("ieee37", "ieee123"):
            result = self._run(feeder_id, REGISTERED_SCENARIOS["parameter_modification"])
            self.assertEqual(result.original_value, 1.0)
            self.assertEqual(result.injected_value, 1.35)
            commands = [e for e in result.events if e.message_type == "COMMAND"]
            reports = [e for e in result.events if e.message_type == "REPORT"]
            self.assertTrue(commands[0].injected)
            self.assertTrue(reports[0].injected)  # tampered value reported as operating value
            self.assertAlmostEqual(reports[0].value, 1.35, places=6)

    def test_scenario_ids_follow_convention(self):
        self.assertEqual(scenario_id_for("reconnaissance", "ieee37"), "reconnaissance_ieee37")
        self.assertEqual(scenario_id_for("parameter_modification", "ieee123"),
                         "parameter_modification_ieee123")


class PhysicalEffectAssertions(ScenarioRealDataTests):
    """Closed switch / parameter modification show real physical deltas."""

    def test_parameter_modification_reports_non_degenerate_deltas(self):
        result = self._run("ieee37", REGISTERED_SCENARIOS["parameter_modification"])
        effect = result.physical_effect
        self.assertEqual(effect["status"], "computed")
        self.assertTrue(effect["affected"]["converged"])
        self.assertIsNotNone(effect["deltas"]["source_p_kw"])
        self.assertNotEqual(effect["deltas"]["source_p_kw"], 0.0)

    def test_unauthorized_command_closing_ieee123_switch_has_real_effect(self):
        result = self._run("ieee123", REGISTERED_SCENARIOS["unauthorized_command"])
        self.assertEqual(result.target.sub_kind, "switch")
        self.assertEqual(result.physical_effect["status"], "computed")
        self.assertNotEqual(result.physical_effect["deltas"]["source_p_kw"], 0.0)


if __name__ == "__main__":
    unittest.main()