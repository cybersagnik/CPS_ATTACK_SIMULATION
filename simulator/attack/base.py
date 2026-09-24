"""The attack abstraction (Task F1).

An :class:`Attack` is a feeder-independent description of one cyber attack on
the simulated CPS.  All scenario logic -- target selection, precondition
validation, execution, event generation and metadata -- is expressed through
this interface so that future attacks can be added without touching the core
engine.

Lifecycle (orchestrated by ``simulator.attack.engine.AttackEngine``)::

    context.ensure_inventory()
        -> validate_preconditions(ctx)      (clean FAILED result if not met)
        -> select_targets(ctx)              (dynamic discovery; FAILED if none)
        -> validate_target(inventory, ...)  (safe-target checks, F3)
        -> execute(ctx, selection)          (attack action + effect)
        -> generate_events(ctx, selection, action)
        -> AttackResult (structured, Task-H-consumable)

The context carries every dependency the attack may read -- the common grid
model, the normal network events (or the CSV path) and a reference timestamp.
No module-level mutable state is ever used.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .events import AttackEvent
from .mitre import MITREMapping
from .results import AttackResult
from .targets import Inventory, SelectedTarget, TargetSelection


class AttackError(RuntimeError):
    """Signals an unrecoverable programming/usage error in the attack engine.

    Deliberately *not* used for "the attack could not find a target": that is a
    normal, expected outcome and is reported as a structured failed
    :class:`AttackResult` (fail cleanly, never silently attack something).
    """


@dataclass
class AttackContext:
    """Explicit dependencies for one attack execution.

    At least one of ``events``/``event_path`` (for measurement discovery) or
    ``model`` (for control/parameter discovery) must be provided.  ``inventory``
    may be pre-built (the engine builds it when absent) so scenario runs reuse
    one discovery pass across many attacks.
    """

    scenario_id: str
    feeder_id: str
    model: Optional[object] = None
    events: List[object] = field(default_factory=list)
    event_path: Optional[Path] = None
    timestamp: str = ""
    inventory: Optional[Inventory] = None
    random_seed: int = 42
    config: Dict[str, object] = field(default_factory=dict)
    results_dir: Optional[Path] = None

    def ensure_inventory(self) -> Inventory:
        if self.inventory is None:
            raise AttackError(
                "attack engine requires: model or event data (event_path/events)"
            )
        return self.inventory


@dataclass
class AttackActionResult:
    """What an attack actually *did* (produced by descend and consumed by the
    engine to build the final structured :class:`AttackResult`)."""

    status: str = "executed"
    target: Optional[SelectedTarget] = None
    source_event_id: str = ""
    original_value: Optional[object] = None
    injected_value: Optional[object] = None
    reported_value: Optional[object] = None
    command_id: str = ""
    physical_effect: Dict[str, object] = field(default_factory=dict)
    metadata: Dict[str, object] = field(default_factory=dict)
    failure_reason: str = ""
    #: set True when the resulting REPORT echoes a value the operator cannot
    #: trust (data forgery), defaulting to False for truthful device echoes.
    injected_report: bool = False


class Attack(ABC):
    """Base class for every attack scenario in the dataset pipeline.

    Subclasses set the class-level declaration (``attack_id``, ``name``,
    ``description``, ``category``, ``mitre``) and implement the four lifecycle
    steps.  The engine turns those steps into a structured
    :class:`AttackResult`.
    """

    attack_id: str = ""
    name: str = ""
    description: str = ""
    category: str = ""
    mitre: Tuple[MITREMapping, ...] = ()

    # ------------------------------------------------------------------ #
    # declarations
    # ------------------------------------------------------------------ #
    def metadata(self) -> Dict[str, object]:
        """Attack-level metadata (independent of any feeder/data)."""
        return {
            "attack_id": self.attack_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "mitre": [m.as_dict() for m in self.mitre],
        }

    @abstractmethod
    def select_targets(self, context: AttackContext) -> TargetSelection:
        """Discover and select a compatible target from the available data/model."""

    @abstractmethod
    def validate_preconditions(self, context: AttackContext) -> List[str]:
        """Return a list of unmet preconditions (empty means all satisfied)."""

    @abstractmethod
    def execute(
        self,
        context: AttackContext,
        selection: TargetSelection,
    ) -> AttackActionResult:
        """Perform the attack against ``selection.target`` and return the action."""

    def generate_events(
        self,
        context: AttackContext,
        action: AttackActionResult,
    ) -> List[AttackEvent]:
        """Turn the executed action into attack network events.

        Default: the action's events are already materialised by ``execute``
        and copied here; scenarios that emit events during ``execute`` may
        override with their own generation logic.
        """
        events: List[AttackEvent] = []
        if action.target is not None:
            events.extend(self._default_events(context, action))
        return events

    # ------------------------------------------------------------------ #
    # helpers for subclasses
    # ------------------------------------------------------------------ #
    def _default_events(
        self,
        context: AttackContext,
        action: AttackActionResult,
    ) -> List[AttackEvent]:
        """A COMMAND + REPORT pair describing an executed action (subclasses
        may call or override)."""
        from .events import (  # local import to avoid a cycle at module load
            DEVICE_SCADA_MASTER,
            DIRECTION_RTU_TO_SCADA,
            DIRECTION_SCADA_TO_RTU,
            MESSAGE_TYPE_COMMAND,
            MESSAGE_TYPE_REPORT,
        )
        from simulator.network.events import rtu_of

        target = action.target
        rtu = rtu_of(context.feeder_id)
        return [
            AttackEvent(
                scenario_id=context.scenario_id,
                timestamp=context.timestamp,
                feeder_id=context.feeder_id,
                src_device=DEVICE_SCADA_MASTER,
                dst_device=rtu,
                message_type=MESSAGE_TYPE_COMMAND,
                direction=DIRECTION_SCADA_TO_RTU,
                point_id=target.point_id,
                value=float(action.injected_value) if action.injected_value is not None else None,
                unit=target.unit,
                injected=True,
                original_value=action.original_value,
                command_id=action.command_id,
                result=action.status,
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
                value=float(action.reported_value) if action.reported_value is not None else None,
                unit=target.unit,
                injected=action.injected_report,
                original_value=action.original_value,
                reported_value=action.reported_value,
                command_id=action.command_id,
                result="APPLIED",
            ),
        ]