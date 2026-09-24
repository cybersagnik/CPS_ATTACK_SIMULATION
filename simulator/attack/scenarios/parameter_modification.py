"""Scenario: Parameter Modification (T0836 Modify Parameter).

The adversary reconfigures a discovered load's operating point by modifying its
``kw_multiplier`` (active power) parameter -- the attacker pushes the feeder
towards an overloaded operating point.  Mapped to MITRE ICS ``T0836 -- Modify
Parameter`` (Impair Process Control).

Selection is feeder-agnostic and dynamic: it targets any controllable load
``kw_multiplier`` / ``kvar_multiplier`` the physical model can represent (with a
``kw_multiplier`` modification, the reactive power rides along at the same
factor so the load power factor is preserved and the effect is honest).  The
modification is deterministic: ``new = baseline + delta`` (default ``delta``
``0.35``).  The original value is preserved in the result/events so the export
layer (Task H) can reconstruct ground truth; the physical solve is built from a
fresh circuit per run, so no state leaks between runs.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..base import Attack, AttackContext, AttackActionResult
from ..mitre import mitre_for
from ..physical import PhysicalSimulationRunner, PhysicalSimulationUnavailable
from ..targets import SelectionRequirement, TargetSelector, TargetSelection

__all__ = ["ParameterModificationAttack"]


class ParameterModificationAttack(Attack):
    attack_id = "parameter_modification"
    name = "Parameter Modification"
    description = (
        "Adversary modifies a discovered load coefficient (kw/kvar multiplier) "
        "to push the feeder towards an overloaded operating point (MITRE ICS T0836)."
    )
    category = "Parameter Modification"
    mitre = (mitre_for("T0836"),)

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
                concepts=("load",),
                parameters=("kw_multiplier", "kvar_multiplier"),
                supported_only=True,
            )
        )

    def execute(self, context: AttackContext, selection: TargetSelection) -> AttackActionResult:
        target = selection.target
        baseline = target.value if isinstance(target.value, (int, float)) else 1.0
        baseline = float(baseline)

        delta = float(context.config.get("modify_delta", 0.35))
        modified = round(baseline + delta, 6)
        asset_token = re.sub(r"[^A-Za-z0-9_]", "_", target.asset_uid)
        command_id = f"PARAM-{self.attack_id.upper()}-{asset_token}-{target.parameter}"

        action = AttackActionResult(
            status="executed",
            target=target,
            original_value=baseline,
            injected_value=modified,
            reported_value=modified,
            command_id=command_id,
            injected_report=True,
            metadata={
                "parameter": target.parameter,
                "baseline": baseline,
                "modified": modified,
                "delta": delta,
                "modify_delta_config": delta,
            },
            physical_effect=self._physical(context, target, modified),
        )
        return action

    @staticmethod
    def _physical(context: AttackContext, target, modified: float) -> Dict[str, object]:
        if context.model is None:
            return {"status": "unavailable", "reason": "no model to solve"}
        uid = target.asset_uid
        # kw + kvar scale together to preserve the power factor of the load.
        overrides = {uid: {"kw_multiplier": modified, "kvar_multiplier": modified}}
        try:
            runner = PhysicalSimulationRunner(context.model)
            baseline = runner.solve()
            affected = runner.solve(overrides)

            def _delta(a, b):
                if a is None or b is None:
                    return None
                return b - a

            effect: Dict[str, object] = {
                "status": "computed",
                "override": overrides,
                "target_bus_refs": list(target.bus_refs),
                "baseline": {
                    "vpu_min": baseline.vpu_min,
                    "vpu_max": baseline.vpu_max,
                    "source_p_kw": baseline.source_p_kw,
                    "source_q_kvar": baseline.source_q_kvar,
                    "converged": baseline.converged,
                },
                "affected": {
                    "vpu_min": affected.vpu_min,
                    "vpu_max": affected.vpu_max,
                    "source_p_kw": affected.source_p_kw,
                    "source_q_kvar": affected.source_q_kvar,
                    "converged": affected.converged,
                },
                "deltas": {
                    "vpu_min": _delta(baseline.vpu_min, affected.vpu_min),
                    "vpu_max": _delta(baseline.vpu_max, affected.vpu_max),
                    "source_p_kw": _delta(baseline.source_p_kw, affected.source_p_kw),
                },
            }
            effect["affected_point"] = ParameterModificationAttack._affected_point(
                context, target, affected
            )
            return effect
        except PhysicalSimulationUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover - engine dependent
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _affected_point(context: AttackContext, target, affected) -> Optional[Dict[str, object]]:
        """The reported voltage point on the modified load's bus (before/after)."""
        bus_token = None
        for ref in target.bus_refs:
            token = str(ref).split(".")[0]
            if not bus_token:
                bus_token = token
                break
        if not bus_token:
            return None
        voltage = [
            p for p in context.inventory.measurement_points
            if p.kind == "voltage" and p.point_id.startswith("BUS_")
            and p.point_id.split("_")[1] == bus_token
        ]
        if not voltage:
            return None
        point = sorted(voltage, key=lambda p: p.point_id)[0]
        return {
            "point_id": point.point_id,
            "bus": bus_token,
            "vpu_before": point.reference_value,
            "vpu_after": affected.vpu_of_bus(bus_token),
            "unit": point.unit,
        }