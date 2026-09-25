"""Scenario: False Measurement (T0856 Spoof Reporting Message).

The adversary forges the RTU->SCADA reporting message for one real measurement
point so the operator observes a value that differs from the physical truth.
The grid itself is untouched: physical deltas are genuinely zero, which is the
*feature* of this scenario -- the network/cyber truth and the physical truth
deliberately tell different stories (the README Scenario 5 contract).

The forged value is ``actual * measurement_scale`` (default ``1.05``; config key
``measurement_scale``), deterministic and feeder-agnostic.  Selection picks a
numeric measurement point (prefer voltage, else current, else power) that the
normal network events actually report; no point id is hardcoded.

The event footprint is a forged READ dialog: an injected ``QUERY`` plus one
injected ``REPORT`` carrying ``original_value`` (physical truth) and
``reported_value`` (what the operator sees).  The physical effect is computed
from the real feeder model and is honestly zero -- no fabricated number.
"""

from __future__ import annotations

import re
from typing import Dict, List

from ..base import Attack, AttackContext, AttackActionResult
from ..events import (
    DEVICE_SCADA_MASTER,
    DIRECTION_RTU_TO_SCADA,
    DIRECTION_SCADA_TO_RTU,
    MESSAGE_TYPE_QUERY,
    MESSAGE_TYPE_REPORT,
    AttackEvent,
    rtu_of,
)
from ..mitre import mitre_for
from ..physical import PhysicalSimulationRunner, PhysicalSimulationUnavailable, effect_snapshot
from ..targets import SelectionRequirement, TargetSelector, TargetSelection

__all__ = ["FalseMeasurementAttack"]


class FalseMeasurementAttack(Attack):
    attack_id = "false_measurement"
    name = "False Measurement"
    description = (
        "Adversary forges an RTU->SCADA reporting message so the operator sees a "
        "measurement that differs from the physical truth, while the grid itself "
        "is never changed (MITRE ICS T0856)."
    )
    category = "False Measurement"
    mitre = (mitre_for("T0856"),)

    def validate_preconditions(self, context: AttackContext) -> List[str]:
        inventory = context.ensure_inventory()
        unmet: List[str] = []
        numeric = [p for p in inventory.measurement_points if p.reference_value is not None]
        if not numeric:
            unmet.append("no numeric measurement point available to spoof")
        if not context.timestamp:
            unmet.append("context timestamp is required")
        return unmet

    def select_targets(self, context: AttackContext) -> TargetSelection:
        selector = TargetSelector(context.inventory, seed=context.random_seed)
        return selector.select(
            SelectionRequirement(
                kind="measurement",
                sub_kinds=("voltage", "current", "power"),
                numeric=True,
                prefer=("voltage", "current", "power"),
            )
        )

    def execute(self, context: AttackContext, selection: TargetSelection) -> AttackActionResult:
        target = selection.target
        baseline = float(target.value) if isinstance(target.value, (int, float)) else None
        if baseline is None:
            raise RuntimeError("selected measurement target has no numeric value")

        scale = float(context.config.get("measurement_scale", 1.05))
        reported = round(baseline * scale, 6)
        point_token = re.sub(r"[^A-Za-z0-9_]", "_", target.point_id)
        command_id = f"READ-{context.feeder_id}-{point_token}"

        return AttackActionResult(
            status="executed",
            target=target,
            original_value=baseline,
            injected_value=reported,
            reported_value=reported,
            command_id=command_id,
            injected_report=True,
            metadata={
                "physical_truth_point": target.point_id,
                "point_kind": target.sub_kind,
                "true_value": baseline,
                "reported_value": reported,
                "measurement_scale": scale,
                "cyber_matches_physical": baseline == reported,
            },
            physical_effect=self._physical(context, target, baseline, reported),
        )

    def generate_events(self, context: AttackContext, action: AttackActionResult) -> List[AttackEvent]:
        target = action.target
        rtu = rtu_of(context.feeder_id)
        point_token = re.sub(r"[^A-Za-z0-9_]", "_", target.point_id)
        command_id = f"READ-{context.feeder_id}-{point_token}"
        return [
            AttackEvent(
                scenario_id=context.scenario_id,
                timestamp=context.timestamp,
                feeder_id=context.feeder_id,
                src_device=DEVICE_SCADA_MASTER,
                dst_device=rtu,
                message_type=MESSAGE_TYPE_QUERY,
                direction=DIRECTION_SCADA_TO_RTU,
                injected=True,
                command_id=command_id,
                result="READ",
            ),
            AttackEvent(
                scenario_id=context.scenario_id,
                timestamp=context.timestamp,
                feeder_id=context.feeder_id,
                src_device=rtu,
                dst_device=DEVICE_SCADA_MASTER,
                message_type=MESSAGE_TYPE_REPORT,
                direction=DIRECTION_RTU_TO_SCADA,
                point_id=target.point_id,
                value=float(action.reported_value),
                unit=target.unit,
                injected=True,
                original_value=action.original_value,
                reported_value=action.reported_value,
                command_id=command_id,
                result="SPOOFED",
            ),
        ]

    @staticmethod
    def _physical(
        context: AttackContext, target, true_value: float, reported_value: float
    ) -> Dict[str, object]:
        if context.model is None:
            return {"status": "unavailable", "reason": "no model to solve"}
        try:
            runner = PhysicalSimulationRunner(context.model)
            effect = effect_snapshot(runner, {})
            effect["status"] = "computed"
            effect["override"] = {}
            effect["physical_truth_point"] = target.point_id
            effect["true_value"] = true_value
            effect["reported_value"] = reported_value
            effect["note"] = (
                "reporting forged only; no physical change, deltas are the honest zero"
            )
            return effect
        except PhysicalSimulationUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover - engine dependent
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}