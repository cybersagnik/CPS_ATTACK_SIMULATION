"""Scenario: Communication Disruption (T0804 Block Reporting Message).

The adversary prevents the genuine reporting message (RTU -> SCADA_MASTER) of
one measurement point from reaching the operator.  The field measurement is
taken truthfully -- the REPORT row carries the real physical value with
``delivery_status="DROPPED"`` -- but it never reaches SCADA, so the operator
loses visibility while the grid keeps operating (README Scenario 6 contract:
message loss, device state, grid state).

The event footprint is ``QUERY (result=BLOCK) -> REPORT (delivery_status=DROPPED,
result=BLOCKED)``: the injected QUERY models the instruction to a compromised
RTU to withhold point reporting, and the blocked REPORT records the genuine
measurement that did not arrive.  The physical effect is computed from the real
feeder model and is honestly zero -- communication was severed, no physical
parameter was changed.  Selection is feeder-agnostic and dynamic.
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

__all__ = ["CommunicationDisruptionAttack"]


class CommunicationDisruptionAttack(Attack):
    attack_id = "communication_disruption"
    name = "Communication Disruption"
    description = (
        "Adversary blocks the reporting message of a real measurement point so "
        "SCADA never receives it; the grid keeps operating, the operator "
        "loses visibility (MITRE ICS T0804)."
    )
    category = "Communication Disruption"
    mitre = (mitre_for("T0804"),)

    def validate_preconditions(self, context: AttackContext) -> List[str]:
        inventory = context.ensure_inventory()
        unmet: List[str] = []
        numeric = [p for p in inventory.measurement_points if p.reference_value is not None]
        if not numeric:
            unmet.append("no numeric measurement point available to block")
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

        point_token = re.sub(r"[^A-Za-z0-9_]", "_", target.point_id)
        command_id = f"JAM-{context.feeder_id}-{point_token}"

        return AttackActionResult(
            status="executed",
            target=target,
            original_value=baseline,
            injected_value=baseline,
            reported_value=baseline,
            command_id=command_id,
            metadata={
                "blocked_point": target.point_id,
                "point_kind": target.sub_kind,
                "true_value": baseline,
                "delivery_status": "DROPPED",
                "message_loss": True,
                "command_failure": False,
                "connection_failure": False,
            },
            physical_effect=self._physical(context, target, baseline),
        )

    def generate_events(self, context: AttackContext, action: AttackActionResult) -> List[AttackEvent]:
        target = action.target
        rtu = rtu_of(context.feeder_id)
        point_token = re.sub(r"[^A-Za-z0-9_]", "_", target.point_id)
        command_id = f"JAM-{context.feeder_id}-{point_token}"
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
                result="BLOCK",
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
                injected=False,
                delivery_status="DROPPED",
                original_value=action.original_value,
                reported_value=action.reported_value,
                command_id=command_id,
                result="BLOCKED",
            ),
        ]

    @staticmethod
    def _physical(
        context: AttackContext, target, true_value: float
    ) -> Dict[str, object]:
        if context.model is None:
            return {"status": "unavailable", "reason": "no model to solve"}
        try:
            runner = PhysicalSimulationRunner(context.model)
            effect = effect_snapshot(runner, {})
            effect["status"] = "computed"
            effect["override"] = {}
            effect["blocked_point"] = target.point_id
            effect["true_value"] = true_value
            effect["note"] = (
                "reporting blocked only; grid keeps operating, deltas are the honest zero"
            )
            return effect
        except PhysicalSimulationUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover - engine dependent
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}