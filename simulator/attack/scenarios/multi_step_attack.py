"""Scenario: Multi-Step Attack (T0846 + T0855 + T0836).

The adversary chains several techniques into one attack *timeline* instead of
isolated malicious rows (README Scenario 7: Access -> Recon -> Target -> Modify
-> Observe).  The scenario composes the three registered single-step attack
scenarios and replays each through the same AttackEngine with distinct stage
timestamps (+1s per stage) so the exported event timeline is strictly
chronological:

1. ``reconnaissance``          (T0846) at ``t``    -- discover the RTU surface;
2. ``unauthorized_command``    (T0855) at ``t+1s`` -- issue an unauthorized
   state change to a discovered device;
3. ``parameter_modification``  (T0836) at ``t+2s`` -- push a load coefficient
   outside the normal envelope (observe the result in the final footprint).

The union of the three techniques is declared as the scenario's MITRE mapping.
Preconditions are the composite of all three stages and are verified *before*
any execution, so the engine fails cleanly if any stage cannot run.  Every
stage shares the multi-step ``scenario_id`` (one timeline is one scenario).
The combined physical effect solves the real feeder once at baseline and once
with the union of every modifying stage's overrides -- the honest end-state
footprint of the whole timeline, never a fabricated number.  All imports of the
composed scenarios/engine are lazy to avoid a circular import at module load.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List

from ..base import Attack, AttackContext, AttackActionResult, AttackError
from ..mitre import mitre_for
from ..results import RESULT_SUCCESS
from ..targets import SelectionRequirement, TargetSelector, TargetSelection
from ..physical import PhysicalSimulationRunner, PhysicalSimulationUnavailable, effect_snapshot

__all__ = ["MultiStepAttack"]


def _step_timestamp(timestamp: str, seconds: int) -> str:
    """Chronological ISO timestamp ``seconds`` after ``timestamp`` (or it back,
    verbatim, if it cannot be parsed).  Zero-padded ISO sorts like time."""
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return timestamp
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


class MultiStepAttack(Attack):
    attack_id = "multi_step_attack"
    name = "Multi-Step Attack"
    description = (
        "Adversary chains reconnaissance -> unauthorized command -> parameter "
        "modification into one chronological attack timeline "
        "(MITRE ICS T0846 + T0855 + T0836)."
    )
    category = "Multi-Step Attack"
    mitre = (mitre_for("T0846"), mitre_for("T0855"), mitre_for("T0836"))

    #: composed single-step stages, in execution order (by attack id).
    STAGE_IDS = ("reconnaissance", "unauthorized_command", "parameter_modification")
    STAGE_OFFSET_SECONDS = 1

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def validate_preconditions(self, context: AttackContext) -> List[str]:
        from . import REGISTERED_SCENARIOS

        inventory = context.ensure_inventory()
        unmet: List[str] = []
        if context.model is None:
            unmet.append("multi-step requires the feeder model (control stages need it)")
        if not context.timestamp:
            unmet.append("context timestamp is required")
        if not inventory.measurement_points:
            unmet.append("no telemetry data available for the reconnaissance stage")
        commandable = [
            p for p in inventory.control_points
            if p.concept in ("switch", "capacitor", "load")
            and p.parameter in ("closed", "switched", "enabled")
            and p.physically_supported
        ]
        if not commandable:
            unmet.append(
                "no physically supported commandable asset for the "
                "unauthorized-command stage (T0855)"
            )
        loadable = [
            p for p in inventory.control_points
            if p.concept == "load"
            and p.parameter in ("kw_multiplier", "kvar_multiplier")
            and p.physically_supported
        ]
        if not loadable:
            unmet.append(
                "no physically supported load coefficient for the "
                "parameter-modification stage (T0836)"
            )
        for stage_id in self.STAGE_IDS:
            if stage_id not in REGISTERED_SCENARIOS:
                unmet.append(f"composed stage {stage_id!r} is not registered")
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
        from . import REGISTERED_SCENARIOS
        from ..engine import AttackEngine

        inventory = context.ensure_inventory()
        engine = AttackEngine(seed=context.random_seed)
        stage_results = []
        stage_events: List[List[object]] = []
        combined_overrides: Dict[str, Dict[str, object]] = {}

        for offset, stage_id in enumerate(self.STAGE_IDS):
            stage_timestamp = _step_timestamp(
                context.timestamp, offset * self.STAGE_OFFSET_SECONDS
            )
            stage_context = AttackContext(
                scenario_id=context.scenario_id,
                feeder_id=context.feeder_id,
                model=context.model,
                inventory=inventory,
                timestamp=stage_timestamp,
                random_seed=context.random_seed,
                config=dict(context.config),
            )
            stage = REGISTERED_SCENARIOS[stage_id]
            stage_result = engine.run(stage, stage_context)
            if stage_result.result != RESULT_SUCCESS:
                raise AttackError(
                    f"multi-step stage {stage_id!r} failed: "
                    f"{stage_result.failure_reason or stage_result.result}"
                )
            stage_results.append(stage_result)
            stage_events.append(list(stage_result.events))
            override = stage_result.physical_effect.get("override")
            if isinstance(override, dict):
                for uid, params in override.items():
                    if isinstance(params, dict):
                        combined_overrides.setdefault(uid, {}).update(params)

        final = stage_results[-1]
        return AttackActionResult(
            status="executed",
            target=selection.target,
            original_value=final.original_value,
            injected_value=final.injected_value,
            reported_value=final.reported_value,
            command_id=final.command_id,
            metadata={
                "stage_count": len(stage_results),
                "stages": [
                    {
                        "attack_id": result.attack_id,
                        "status": result.status,
                        "target_point_id": result.target.point_id if result.target else "",
                        "target_kind": result.target.kind if result.target else "",
                        "target_sub_kind": result.target.sub_kind if result.target else "",
                        "command_id": result.command_id,
                        "original_value": result.original_value,
                        "injected_value": result.injected_value,
                        "reported_value": result.reported_value,
                        "event_count": len(result.events),
                        "physical_deltas": self._deltas_summary(result.physical_effect),
                    }
                    for result in stage_results
                ],
                "_stage_events": stage_events,
            },
            physical_effect=self._combined_physical(context, combined_overrides),
        )

    def generate_events(
        self, context: AttackContext, action: AttackActionResult
    ) -> List:
        flattened = []
        for stage_events in action.metadata.get("_stage_events", []):
            flattened.extend(stage_events)
        return flattened

    # ------------------------------------------------------------------ #
    # combined footprint
    # ------------------------------------------------------------------ #
    @staticmethod
    def _combined_physical(
        context: AttackContext, overrides: Dict[str, Dict[str, object]]
    ) -> Dict[str, object]:
        if not overrides:
            return {"status": "none", "reason": "no stage produced a physical override"}
        if context.model is None:
            return {"status": "unavailable", "reason": "no model to solve"}
        try:
            runner = PhysicalSimulationRunner(context.model)
            effect = effect_snapshot(runner, overrides)
            effect["status"] = "computed"
            effect["override"] = overrides
            effect["note"] = "combined footprint of every modifying stage in the timeline"
            return effect
        except PhysicalSimulationUnavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover - engine dependent
            return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _deltas_summary(effect: Dict[str, object]) -> Dict[str, object]:
        deltas = effect.get("deltas", {})
        return {k: v for k, v in deltas.items() if v is not None}