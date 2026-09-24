"""Scenario: Unauthorized Command (T0855 Unauth. Command Message).

The adversary poses as the SCADA master and issues an *unauthorized* state
change to a discovered controllable device -- switching a normally-closed
sectionalizer open, dropping a switched capacitor bank, or disconnecting a
load.  Mapped to MITRE ICS ``T0855 -- Unauthorized Command Message`` (Impair
Process Control).

Target selection is dynamic and feeder-agnostic: it chooses the first
*available* of ``switch.closed``, ``capacitor.switched``, ``load.enabled`` that
the physical model can honour (any feeder yields at least one).  If no such
commandable asset exists, the engine returns a structured FAILED result -- an
actual (not invented) switch/capacitor/load is always required.

The commanded state is the opposite of the device's legitimate current state,
i.e. the unauthorized direction.  The physical effect (voltage/power footprint)
is computed by ``simulator.attack.physical`` when available and reported as
``physical_effect``; otherwise the reason is recorded -- never a fabricated
number.
"""

from __future__ import annotations

import re
from typing import Dict, List

from ..base import Attack, AttackContext, AttackActionResult
from ..mitre import mitre_for
from ..physical import PhysicalSimulationRunner, PhysicalSimulationUnavailable, effect_snapshot
from ..targets import SelectionRequirement, TargetSelector, TargetSelection

__all__ = ["UnauthorizedCommandAttack"]

_STATE_LABEL = {True: "close", False: "open"}


class UnauthorizedCommandAttack(Attack):
    attack_id = "unauthorized_command"
    name = "Unauthorized Command"
    description = (
        "Adversary issues an unauthorized state-change command to a discovered "
        "controllable device (switch / capacitor / load) (MITRE ICS T0855)."
    )
    category = "Unauthorized Command"
    mitre = (mitre_for("T0855"),)

    def validate_preconditions(self, context: AttackContext) -> List[str]:
        if context.model is None:
            return ["no controllable asset model available (context.model missing)"]
        if not context.timestamp:
            return ["context timestamp is required"]
        return []

    def select_targets(self, context: AttackContext) -> TargetSelection:
        selector = TargetSelector(context.inventory, seed=context.random_seed)
        return selector.select(
            SelectionRequirement(
                kind="control",
                concepts=("switch", "capacitor", "load"),
                parameters=("closed", "switched", "enabled"),
                supported_only=True,
                prefer=("switch", "capacitor", "load"),
            )
        )

    def execute(self, context: AttackContext, selection: TargetSelection) -> AttackActionResult:
        target = selection.target
        inventory = context.inventory
        point = inventory.control(target.point_id)
        baseline = target.value
        if baseline is None:
            baseline = True
        baseline = bool(baseline)

        commanded = not baseline  # unauthorized direction: opposite of legitimate state
        asset_token = re.sub(r"[^A-Za-z0-9_]", "_", target.asset_uid)
        command_id = f"CMD-{self.attack_id.upper()}-{asset_token}-{target.parameter}"

        action = AttackActionResult(
            status="executed",
            target=target,
            original_value=baseline,
            injected_value=commanded,
            reported_value=commanded,
            command_id=command_id,
            metadata={
                "command": f"{_STATE_LABEL[commanded]} {point.concept} {target.asset_source_id}",
                "parameter": target.parameter,
                "baseline_state": baseline,
                "commanded_state": commanded,
                "concept": point.concept,
            },
            physical_effect=self._physical(context, target.asset_uid, target.parameter, commanded),
        )
        return action

    @staticmethod
    def _physical(context: AttackContext, uid: str, parameter: str, value: bool) -> Dict[str, object]:
        if context.model is None:
            return {"status": "unavailable", "reason": "no model to solve"}
        try:
            runner = PhysicalSimulationRunner(context.model)
            effect = effect_snapshot(runner, {uid: {parameter: value}})
            effect["status"] = "computed"
            effect["override"] = {uid: {parameter: value}}
            return effect
        except PhysicalSimulationUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover - engine dependent
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}