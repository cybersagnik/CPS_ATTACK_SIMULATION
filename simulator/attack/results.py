"""Structured attack execution result and ground truth (F4).

:class:`AttackResult` is the single structured object every scenario produces.
Future Task H consumes it directly: ``result.as_dict()`` is a flat
ground-truth record and ``result.event_rows()`` are the attack network events
in the documented attack-event CSV schema.  Nothing here persists anything --
export belongs to Task H.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .events import AttackEvent
from .mitre import MITREMapping
from .targets import SelectedTarget, TargetSelection

__all__ = ["AttackResult", "RESULT_SUCCESS", "RESULT_FAILED"]

RESULT_SUCCESS = "SUCCESS"
RESULT_FAILED = "FAILED"

#: failure reasons normalised for machine consumption.
REASON_PRECONDITION = "precondition_failed"
REASON_NO_TARGET = "no_compatible_target"
REASON_TARGET_INVALID = "target_invalid"


@dataclass
class AttackResult:
    """One attack execution, represented so Task H can consume it later."""

    scenario_id: str
    attack_id: str
    attack_name: str
    category: str
    feeder_id: str
    timestamp: str
    result: str = RESULT_SUCCESS
    status: str = "executed"
    target: Optional[SelectedTarget] = None
    selection: Optional[TargetSelection] = None
    source_event_id: str = ""
    original_value: Optional[object] = None
    injected_value: Optional[object] = None
    reported_value: Optional[object] = None
    command_id: str = ""
    mitre: Tuple[MITREMapping, ...] = ()
    events: List[AttackEvent] = field(default_factory=list)
    physical_effect: Dict[str, object] = field(default_factory=dict)
    metadata: Dict[str, object] = field(default_factory=dict)
    failure_reason: str = ""

    # ------------------------------------------------------------------ #
    # ground truth / consumption surface (Task H)
    # ------------------------------------------------------------------ #
    def ground_truth(self) -> Dict[str, object]:
        """One flat, machine-readable ground-truth record."""
        target = self.target.as_dict() if self.target is not None else {}
        return {
            "scenario_id": self.scenario_id,
            "attack_id": self.attack_id,
            "attack_name": self.attack_name,
            "category": self.category,
            "feeder_id": self.feeder_id,
            "timestamp": self.timestamp,
            "is_attack": 1 if self.result == RESULT_SUCCESS else 0,
            "result": self.result,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "target_point_id": target.get("point_id", ""),
            "target_kind": target.get("kind", ""),
            "target_sub_kind": target.get("sub_kind", ""),
            "target_asset_uid": target.get("asset_uid", ""),
            "target_asset_source_id": target.get("asset_source_id", ""),
            "target_parameter": target.get("parameter", ""),
            "target_bus_refs": ";".join(target.get("bus_refs", [])),
            "target_value_baseline": target.get("value", ""),
            "target_selection_reason": target.get("why", ""),
            "selection_status": self.selection.status if self.selection else "",
            "compatible_candidate_count": (
                len(self.selection.candidates) if self.selection else 0
            ),
            "source_event_id": self.source_event_id,
            "original_value": self.original_value,
            "injected_value": self.injected_value,
            "reported_value": self.reported_value,
            "command_id": self.command_id,
            "mitre_technique_ids": ";".join(m.technique_id for m in self.mitre),
            "mitre_techniques": ";".join(m.technique_name for m in self.mitre),
            "mitre_tactics": ";".join(m.tactic for m in self.mitre),
        }

    def as_dict(self) -> Dict[str, object]:
        """Full structured result (events and effect included)."""
        data = self.ground_truth()
        data.update(
            {
                "mitre": [m.as_dict() for m in self.mitre],
                "metadata": dict(self.metadata),
                "physical_effect": dict(self.physical_effect),
                "event_count": len(self.events),
                "events": [e.as_dict() for e in self.events],
            }
        )
        return data

    def event_rows(self) -> List[Dict[str, object]]:
        """Attack network events as row dicts (attack-event CSV schema)."""
        return [e.as_dict() for e in self.events]

    @classmethod
    def failed(
        cls,
        scenario_id: str,
        attack_id: str,
        attack_name: str,
        category: str,
        feeder_id: str,
        timestamp: str,
        reason: str,
        status: str,
        selection: Optional[TargetSelection] = None,
        mitre: Tuple[MITREMapping, ...] = (),
        metadata: Optional[Dict[str, object]] = None,
    ) -> "AttackResult":
        """A clean, structured failure (no silent random attack)."""
        return cls(
            scenario_id=scenario_id,
            attack_id=attack_id,
            attack_name=attack_name,
            category=category,
            feeder_id=feeder_id,
            timestamp=timestamp,
            result=RESULT_FAILED,
            status=status,
            selection=selection,
            mitre=mitre,
            metadata=metadata or {},
            failure_reason=reason,
        )

    def report(self) -> List[str]:
        lines = [
            f"attack: {self.attack_id} ({self.attack_name})",
            f"scenario_id: {self.scenario_id}",
            f"feeder_id: {self.feeder_id}",
            f"result: {self.result} ({self.status})",
        ]
        if self.failure_reason:
            lines.append(f"reason: {self.failure_reason}")
        if self.target is not None:
            lines.append(f"target: {self.target.point_id} ({self.target.kind}/{self.target.sub_kind})")
        if self.mitre:
            lines.append(
                "mitre: "
                + "; ".join(f"{m.technique_id} {m.technique_name}" for m in self.mitre)
            )
        lines.append(f"events: {len(self.events)}")
        if self.physical_effect:
            delta = self.physical_effect.get("deltas", {})
            lines.append(
                "physical_effect deltas: "
                + "; ".join(f"{k}={v}" for k, v in delta.items() if v is not None)
            )
        return lines