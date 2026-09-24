"""Attack engine: orchestrates the attack lifecycle (Task F).

``AttackEngine.run(attack, context)`` wires the four steps of the
:class:`~simulator.attack.base.Attack` abstraction together and always returns a
structured :class:`~simulator.attack.results.AttackResult`:

1. build the target inventory from the context's model/events (unless given);
2. validate preconditions -- unmet -> clean FAILED result;
3. dynamically discover and select a compatible target -- none -> clean FAILED
   result with the candidates and the reason;
4. validate the selected target against the inventory (exists / same feeder /
   compatible / usable value) -- invalid -> clean FAILED result;
5. execute the attack, generate its network events, assign deterministic event
   identities and return the full ground-truth result.

``event_id`` for attack events follows ``<scenario_id>-atk<NNN>`` so attack rows
are trivially distinguishable from normal ``...-evNNN`` rows.
"""

from __future__ import annotations

from typing import List, Optional

from .base import Attack, AttackContext, AttackError
from .events import AttackEvent
from .results import AttackResult, RESULT_FAILED, RESULT_SUCCESS
from .targets import Inventory, validate_target

__all__ = ["AttackEngine", "SCENARIOS"]


class AttackEngine:
    """Runs any :class:`Attack` against an :class:`AttackContext`."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    # ------------------------------------------------------------------ #
    # main entry point
    # ------------------------------------------------------------------ #
    def run(self, attack: Attack, context: AttackContext) -> AttackResult:
        declaration = self._declare(attack)
        scenario_id = context.scenario_id
        feeder_id = context.feeder_id
        timestamp = context.timestamp or ""

        mining = self._discover(context, attack)
        if isinstance(mining, AttackResult):
            return mining

        preconditions = attack.validate_preconditions(context)
        if preconditions:
            return AttackResult.failed(
                scenario_id=scenario_id,
                attack_id=attack.attack_id,
                attack_name=attack.name,
                category=attack.category,
                feeder_id=feeder_id,
                timestamp=timestamp,
                reason="; ".join(preconditions),
                status="precondition_failed",
                mitre=attack.mitre,
                metadata=declaration,
            )

        selection = attack.select_targets(context)
        if selection.status != "SELECTED" or selection.target is None:
            return AttackResult.failed(
                scenario_id=scenario_id,
                attack_id=attack.attack_id,
                attack_name=attack.name,
                category=attack.category,
                feeder_id=feeder_id,
                timestamp=timestamp,
                reason=selection.failure_reason or "no compatible target",
                status="no_compatible_target",
                selection=selection,
                mitre=attack.mitre,
                metadata=declaration,
            )

        ok, reason = self._validate(context.inventory, selection.target, context)
        if not ok:
            return AttackResult.failed(
                scenario_id=scenario_id,
                attack_id=attack.attack_id,
                attack_name=attack.name,
                category=attack.category,
                feeder_id=feeder_id,
                timestamp=timestamp,
                reason=reason,
                status="target_invalid",
                selection=selection,
                mitre=attack.mitre,
                metadata=declaration,
            )

        action = attack.execute(context, selection)
        events = self._finalize_events(attack, context, action)
        return AttackResult(
            scenario_id=scenario_id,
            attack_id=attack.attack_id,
            attack_name=attack.name,
            category=attack.category,
            feeder_id=feeder_id,
            timestamp=timestamp or selection.target.reference_timestamp,
            result=RESULT_SUCCESS,
            status=action.status,
            target=action.target or selection.target,
            selection=selection,
            source_event_id=action.source_event_id,
            original_value=action.original_value,
            injected_value=action.injected_value,
            reported_value=action.reported_value,
            command_id=action.command_id,
            mitre=attack.mitre,
            events=events,
            physical_effect=dict(action.physical_effect),
            metadata={**declaration, **action.metadata},
        )

    # ------------------------------------------------------------------ #
    # stage helpers
    # ------------------------------------------------------------------ #
    def _declare(self, attack: Attack) -> dict:
        if not attack.attack_id:
            raise AttackError(f"{type(attack).__name__} must declare attack_id")
        if not attack.mitre:
            raise AttackError(f"attack {attack.attack_id!r} must declare a MITRE mapping")
        return attack.metadata()

    def _discover(self, context: AttackContext, attack: Attack) -> Optional[AttackResult]:
        """Build the inventory if the context did not provide one."""
        if context.inventory is not None:
            return None
        from .targets import build_inventory

        rows = None
        if context.event_path is not None:
            inventory = build_inventory(
                feeder_id=context.feeder_id,
                model=context.model,
                events_csv=context.event_path,
                reference_timestamp=context.timestamp or "",
            )
            context.inventory = inventory
            return None
        if context.events:
            rows = []
            for event in context.events:
                if isinstance(event, dict):
                    rows.append(event)
                elif hasattr(event, "as_dict"):
                    rows.append(event.as_dict())
        inventory = build_inventory(
            feeder_id=context.feeder_id,
            model=context.model,
            event_rows=rows if rows else None,
            reference_timestamp=context.timestamp or "",
        )
        context.inventory = inventory
        return None

    def _validate(self, inventory: Inventory, target, context: AttackContext):
        return validate_target(inventory, target, context.feeder_id)

    def _finalize_events(
        self, attack: Attack, context: AttackContext, action
    ) -> List[AttackEvent]:
        point_to_event: dict = {}
        if context.events:
            for event in context.events:
                pid = getattr(event, "point_id", "")
                eid = getattr(event, "event_id", None)
                if pid and eid:
                    point_to_event.setdefault(pid, eid)
        if not action.source_event_id and action.target is not None:
            reference = getattr(action.target, "reference_event_id", "")
            if reference:
                action.source_event_id = reference
            elif action.target.kind == "measurement":
                action.source_event_id = point_to_event.get(action.target.point_id, "")
                if not action.source_event_id and context.inventory is not None:
                    action.source_event_id = (
                        context.inventory.measurement(action.target.point_id).first_event_id
                        if context.inventory.measurement(action.target.point_id)
                        else ""
                    )

        raw = list(attack.generate_events(context, action))
        ordered = sorted(raw, key=lambda e: e.sort_key())
        finalized: List[AttackEvent] = []
        for index, event in enumerate(ordered, start=1):
            finalized.append(
                event.with_identity(
                    event_id=f"{context.scenario_id}-atk{index:03d}",
                    sequence=index,
                )
            )
        return finalized


#: Registered scenarios, keyed by attack id -- the discovery surface for Task H
#: (and for the scenario CLI/validation runs).
SCENARIOS: dict = {}

try:
    from .scenarios import REGISTERED_SCENARIOS  # noqa: F401

    SCENARIOS.update(REGISTERED_SCENARIOS)
except ImportError:  # pragma: no cover - scenarios package missing in isolated use
    pass


def register_scenario(attack: Attack) -> Attack:
    SCENARIOS[attack.attack_id] = attack
    return attack