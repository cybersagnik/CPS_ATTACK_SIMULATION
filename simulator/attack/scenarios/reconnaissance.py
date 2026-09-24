"""Scenario: Reconnaissance (T0846 Remote System Discovery).

The adversary interrogates the RTU of the feeder to discover the SCADA
endpoints, the reported measurement points and the controllable device surface.
The attack is network-only: ``QUERY`` dialogs (no payload) plus ``REPORT``
rows echoing the discovered point inventory.  It is mapped to MITRE ICS
``T0846 -- Remote System Discovery`` (Discovery).

Nothing is modified.  No target exists or no data has surfaced -> structured
FAILED result, never a fabricated discovery.
"""

from __future__ import annotations

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
from ..targets import SelectionRequirement, TargetSelector, TargetSelection

__all__ = ["ReconnaissanceAttack"]


class ReconnaissanceAttack(Attack):
    attack_id = "reconnaissance"
    name = "Reconnaissance / Remote System Discovery"
    description = (
        "Adversary interrogates the feeder RTU to enumerate SCADA endpoints, "
        "reported telemetry points and the controllable device surface "
        "(MITRE ICS T0846)."
    )
    category = "Reconnaissance"
    mitre = (mitre_for("T0846"),)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def validate_preconditions(self, context: AttackContext) -> List[str]:
        inventory = context.ensure_inventory()
        unmet: List[str] = []
        if not inventory.measurement_points and not inventory.control_points:
            unmet.append("no discoverable SCADA data (empty inventory)")
        if not context.timestamp:
            unmet.append("context timestamp is required")
        return unmet

    def select_targets(self, context: AttackContext) -> TargetSelection:
        selector = TargetSelector(context.inventory, seed=context.random_seed)
        return selector.select(
            SelectionRequirement(
                kind="scope",
                sub_kinds=("endpoint",),
                supported_only=True,
            )
        )

    def execute(
        self, context: AttackContext, selection: TargetSelection
    ) -> AttackActionResult:
        inventory = context.inventory
        discovered = self._discover(inventory)
        action = AttackActionResult(
            status="discovered",
            target=selection.target,
            metadata={
                "discovered_assets": discovered,
                "interrogated_endpoint": selection.target.point_id,
            },
        )
        return action

    def generate_events(self, context: AttackContext, action: AttackActionResult) -> List[AttackEvent]:
        inventory = context.inventory
        rtu = rtu_of(context.feeder_id)
        events: List[AttackEvent] = []

        # 1. An interrogation dialog against the discovered endpoint surface.
        events.append(
            AttackEvent(
                scenario_id=context.scenario_id,
                timestamp=context.timestamp,
                feeder_id=context.feeder_id,
                src_device=DEVICE_SCADA_MASTER,
                dst_device=rtu,
                message_type=MESSAGE_TYPE_QUERY,
                direction=DIRECTION_SCADA_TO_RTU,
                injected=True,
                command_id=f"SCAN-{context.feeder_id}-POINTS",
                result="SCAN",
            )
        )
        if inventory.control_points:
            events.append(
                AttackEvent(
                    scenario_id=context.scenario_id,
                    timestamp=context.timestamp,
                    feeder_id=context.feeder_id,
                    src_device=DEVICE_SCADA_MASTER,
                    dst_device=rtu,
                    message_type=MESSAGE_TYPE_QUERY,
                    direction=DIRECTION_SCADA_TO_RTU,
                    injected=True,
                    command_id=f"SCAN-{context.feeder_id}-CONTROLS",
                    result="SCAN",
                )
            )

        # 2. One REPORT per unique reported measurement point (the footprint
        #    the adversary now knows), plus one REPORT per *numeric* control
        #    parameter (bool states would need a fabricated number -- they are
        #    conveyed in ``discovered_assets`` instead, never invented here).
        for point in inventory.measurement_points:
            if point.reference_value is None:
                continue
            events.append(
                AttackEvent(
                    scenario_id=context.scenario_id,
                    timestamp=context.timestamp,
                    feeder_id=context.feeder_id,
                    src_device=rtu,
                    dst_device=DEVICE_SCADA_MASTER,
                    message_type=MESSAGE_TYPE_REPORT,
                    direction=DIRECTION_RTU_TO_SCADA,
                    point_id=point.point_id,
                    value=float(point.reference_value),
                    unit=point.unit,
                    result="DISCOVERED",
                )
            )
        for point in inventory.control_points:
            if point.param_kind not in ("float", "int") or point.baseline_value is None:
                continue
            events.append(
                AttackEvent(
                    scenario_id=context.scenario_id,
                    timestamp=context.timestamp,
                    feeder_id=context.feeder_id,
                    src_device=rtu,
                    dst_device=DEVICE_SCADA_MASTER,
                    message_type=MESSAGE_TYPE_REPORT,
                    direction=DIRECTION_RTU_TO_SCADA,
                    point_id=point.point_id,
                    value=float(point.baseline_value),
                    unit=point.unit,
                    result="DISCOVERED",
                )
            )
        return events

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    @staticmethod
    def _discover(inventory) -> Dict[str, object]:
        by_kind = dict(inventory.activity.get("measurement_points_by_kind", {}))
        control_by_concept: Dict[str, int] = {}
        for point in inventory.control_points:
            control_by_concept[point.concept] = control_by_concept.get(point.concept, 0) + 1
        buses: List[str] = []
        elements: List[str] = []
        for point in inventory.measurement_points:
            if point.kind == "voltage" and point.point_id.startswith("BUS_"):
                buses.append(point.point_id.split("_")[1])
            elif point.kind == "current":
                elements.append(point.point_id)
        return {
            "endpoints": list(inventory.activity.get("endpoints", inventory.endpoints)),
            "interrogated_feeder": inventory.feeder_id,
            "measurement_point_count": len(inventory.measurement_points),
            "measurement_points_by_kind": by_kind,
            "distinct_buses_reported": len(sorted(set(buses))),
            "distinct_current_elements_reported": len(sorted(set(elements))),
            "control_point_count": len(inventory.control_points),
            "control_points_by_concept": control_by_concept,
            "controllable_parameter_count": inventory.activity.get(
                "controllable_parameter_count", len(inventory.control_points)
            ),
            "component_concepts": list(inventory.activity.get("component_concepts", [])),
        }