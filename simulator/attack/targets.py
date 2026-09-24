"""Dynamic target discovery, compatibility and selection (feeder-agnostic).

The attack engine never hardcodes a target.  Instead it builds an
:class:`Inventory` from the *actual* data it is given -- the normal network
events (measurement points the SCADA layer actually reported) and/or the common
grid model (controllable assets and their parameters) -- then filters those
points against an attack's requirements and picks one deterministically.

Nothing in this module knows a feeder name, a bus number or a component id:
selection works purely on generic kinds ("voltage", "current", "power",
"switch", "capacitor", "load", ...) and point-id patterns.

Guarantees
----------
* No valid target -> ``TargetSelection(status="FAILED")`` with the reason and
  the compatible candidates; never a silent random pick.
* Deterministic: with a fixed seed the same inventory yields the same pick
  (sorted candidates, ``index = seed % len(candidates)``).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from simulator.network.events import DEVICE_SCADA_MASTER, rtu_of
from .physical import physically_supported

__all__ = [
    "MeasurementPoint",
    "ControlPoint",
    "Inventory",
    "SelectedTarget",
    "TargetSelection",
    "SelectionRequirement",
    "TargetSelector",
    "validate_target",
    "build_inventory",
    "build_point_inventory",
    "build_control_inventory",
    "control_point_id",
]

POINT_KIND_VOLTAGE = "voltage"
POINT_KIND_CURRENT = "current"
POINT_KIND_POWER = "power"
POINT_KIND_BOOLEAN = "boolean"
POINT_KIND_OTHER = "other"


def point_kind(point_id: str) -> str:
    """Generic kind of a network point id (independent of any feeder)."""
    if point_id.startswith("BUS_"):
        return POINT_KIND_VOLTAGE
    if point_id.startswith("CURRENT_"):
        return POINT_KIND_CURRENT
    if point_id.startswith("FEEDER_SOURCE_P") or point_id.startswith("FEEDER_SOURCE_Q"):
        return POINT_KIND_POWER
    if point_id.startswith("FEEDER_VPU_MIN") or point_id.startswith("FEEDER_VPU_MAX"):
        return POINT_KIND_VOLTAGE
    if point_id.startswith("FEEDER_CONVERGED"):
        return POINT_KIND_BOOLEAN
    if point_id.startswith("CTRL_"):
        return POINT_KIND_OTHER
    return POINT_KIND_OTHER


def control_point_id(concept: str, uid: str, parameter: str) -> str:
    """Deterministic control-point id, e.g. ``CTRL_load_load_007_kw_multiplier``."""
    token = re.sub(r"[^A-Za-z0-9_]", "_", uid)
    return f"CTRL_{concept}_{token}_{parameter}"


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MeasurementPoint:
    """One unique telemetry point observed in the network events."""

    point_id: str
    kind: str
    unit: str
    feeder_id: str
    count: int = 0
    reference_value: Optional[float] = None
    reference_timestamp: str = ""
    first_event_id: str = ""

    def selected(self) -> "SelectedTarget":
        return SelectedTarget(
            point_id=self.point_id,
            kind="measurement",
            sub_kind=self.kind,
            feeder_id=self.feeder_id,
            value=self.reference_value,
            unit=self.unit,
            reference_timestamp=self.reference_timestamp,
            reference_event_id=self.first_event_id,
        )


@dataclass(frozen=True)
class ControlPoint:
    """One tunable parameter of a controllable asset from the common model."""

    point_id: str
    concept: str
    asset_uid: str
    asset_source_id: str
    parameter: str
    param_kind: str
    unit: str
    baseline_value: Optional[object]
    bus_refs: Tuple[str, ...]
    physically_supported: bool
    feeders_ok: bool = True

    def selected(self) -> "SelectedTarget":
        return SelectedTarget(
            point_id=self.point_id,
            kind="control",
            sub_kind=self.concept,
            feeder_id="",
            parameter=self.parameter,
            asset_uid=self.asset_uid,
            asset_source_id=self.asset_source_id,
            bus_refs=list(self.bus_refs),
            value=self.baseline_value,
            unit=self.unit,
        )


@dataclass
class Inventory:
    """Everything the attack engine may target in the current data context."""

    feeder_id: str
    measurement_points: List[MeasurementPoint] = field(default_factory=list)
    control_points: List[ControlPoint] = field(default_factory=list)
    endpoints: List[str] = field(default_factory=list)
    activity: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def empty(cls, feeder_id: str) -> "Inventory":
        return cls(feeder_id=feeder_id, endpoints=[DEVICE_SCADA_MASTER, rtu_of(feeder_id)])

    def measurement(self, point_id: str) -> Optional[MeasurementPoint]:
        for point in self.measurement_points:
            if point.point_id == point_id:
                return point
        return None

    def control(self, point_id: str) -> Optional[ControlPoint]:
        for point in self.control_points:
            if point.point_id == point_id:
                return point
        return None

    def measurements_by_kind(self, kind: str) -> List[MeasurementPoint]:
        return [p for p in self.measurement_points if p.kind == kind]

    def control_by(self, concept: str = "", parameter: str = "",
                   supported_only: bool = False) -> List[ControlPoint]:
        out = []
        for p in self.control_points:
            if concept and p.concept != concept:
                continue
            if parameter and p.parameter != parameter:
                continue
            if supported_only and not p.physically_supported:
                continue
            out.append(p)
        return out


def _parse_reference_value(raw: str) -> Optional[float]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value


def build_point_inventory(
    rows: Iterable[Dict[str, str]],
    feeder_id: str,
    reference_timestamp: str = "",
) -> Inventory:
    """Build the measurement inventory from normal network-event rows.

    ``rows`` are the CSV dict rows of a ``normal_<feeder>_network_events.csv``.
    Points are deduplicated by ``point_id``; ``reference_value`` is the value at
    ``reference_timestamp`` when given, else the last observed value.
    """
    inventory = Inventory.empty(feeder_id)
    by_point: Dict[str, Dict[str, object]] = {}
    for row in rows:
        pid = row.get("point_id") or ""
        if not pid or row.get("message_type") != "TELEMETRY":
            continue
        entry = by_point.setdefault(
            pid,
            {
                "kind": point_kind(pid),
                "unit": row.get("unit") or "",
                "count": 0,
                "value": None,
                "at": "",
                "event_id": row.get("event_id") or "",
            },
        )
        entry["count"] = int(entry["count"]) + 1
        if not entry["event_id"]:
            entry["event_id"] = row.get("event_id") or ""
        if reference_timestamp and row.get("timestamp") == reference_timestamp:
            entry["value"] = _parse_reference_value(row.get("value"))
            entry["at"] = reference_timestamp
        else:
            value = _parse_reference_value(row.get("value"))
            if value is not None and (entry["value"] is None or not reference_timestamp):
                entry["value"] = value
                entry["at"] = row.get("timestamp") or ""

    for pid, entry in by_point.items():
        inventory.measurement_points.append(
            MeasurementPoint(
                point_id=pid,
                kind=str(entry["kind"]),
                unit=str(entry["unit"]),
                feeder_id=feeder_id,
                count=int(entry["count"]),
                reference_value=entry["value"] if isinstance(entry["value"], float) else None,
                reference_timestamp=str(entry["at"]),
                first_event_id=str(entry["event_id"]),
            )
        )
    inventory.measurement_points.sort(key=lambda p: p.point_id)

    kinds: Dict[str, int] = {}
    for point in inventory.measurement_points:
        kinds[point.kind] = kinds.get(point.kind, 0) + 1
    inventory.activity.update(
        {
            "measurement_point_count": len(inventory.measurement_points),
            "measurement_points_by_kind": kinds,
        }
    )
    return inventory


_CONTROL_BASELINES = {
    "regulator": {"tap_position": 0, "enabled": True},
    "switch": {"closed": True},
    "capacitor": {"switched": True},
    "load": {"kw_multiplier": 1.0, "kvar_multiplier": 1.0, "enabled": True},
    "generator": {"kw_multiplier": 1.0, "kvar_multiplier": 1.0, "enabled": True},
}


def _baseline_for(asset, parameter: str) -> Optional[object]:
    concept = asset.concept
    if isinstance(getattr(asset, "closed", None), bool) and parameter == "closed":
        return asset.closed
    for spec in asset.control:
        if spec.name == parameter:
            if concept == "regulator" and parameter == "tap_position":
                return 0  # Phase D holds regulators at nominal tap
            if concept == "capacitor" and parameter == "switched":
                return True
            if concept == "load" and parameter in ("kw_multiplier", "kvar_multiplier"):
                return 1.0
            if concept == "load" and parameter == "enabled":
                return True
            if isinstance(getattr(asset, parameter, None), bool):
                return getattr(asset, parameter)
    return _CONTROL_BASELINES.get(concept, {}).get(parameter)


def build_control_inventory(model, feeder_id: str) -> List[ControlPoint]:
    """Control points from the common grid model's controllable assets."""
    points: List[ControlPoint] = []
    for asset, spec in model.controllable_parameters():
        bus_refs = _bus_refs(asset)
        points.append(
            ControlPoint(
                point_id=control_point_id(asset.concept, asset.uid, spec.name),
                concept=asset.concept,
                asset_uid=asset.uid,
                asset_source_id=asset.source_id,
                parameter=spec.name,
                param_kind=spec.kind,
                unit=spec.unit,
                baseline_value=_baseline_for(asset, spec.name),
                bus_refs=bus_refs,
                physically_supported=physically_supported(asset.concept, spec.name),
            )
        )
    points.sort(key=lambda p: p.point_id)
    return points


def _bus_refs(asset) -> Tuple[str, ...]:
    for attr in ("bus", "location"):
        if getattr(asset, attr, ""):
            return (str(getattr(asset, attr)),)
    b1 = getattr(asset, "bus1", "")
    b2 = getattr(asset, "bus2", "")
    return tuple(b for b in (b1, b2) if b)


def build_inventory(
    feeder_id: str,
    model=None,
    events_csv: Optional[Path] = None,
    event_rows: Optional[Iterable[Dict[str, str]]] = None,
    reference_timestamp: str = "",
) -> Inventory:
    """Assemble an inventory from every source provided (all optional)."""
    inventory = Inventory.empty(feeder_id)
    if events_csv is not None:
        with open(events_csv, newline="", encoding="utf-8") as fh:
            event_rows = list(csv.DictReader(fh))
    if event_rows is not None:
        inventory = build_point_inventory(event_rows, feeder_id, reference_timestamp)
    if model is not None:
        inventory.control_points = build_control_inventory(model, feeder_id)
    inventory.endpoints = [DEVICE_SCADA_MASTER, rtu_of(feeder_id)]

    seen = {p.point_id for p in inventory.measurement_points}
    inventory.activity.update({"endpoints": list(inventory.endpoints)})
    if model is not None:
        concepts = sorted(model.supported_device_types())
        inventory.activity["component_concepts"] = concepts
        inventory.activity["controllable_parameter_count"] = len(inventory.control_points)
    inventory.activity["point_id_count"] = len(seen)
    return inventory


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SelectedTarget:
    """A fully validated target, ready for attack execution."""

    point_id: str
    kind: str  # "measurement" | "control" | "scope"
    sub_kind: str = ""  # e.g. "voltage" | "switch" | "endpoint"
    feeder_id: str = ""
    parameter: str = ""
    asset_uid: str = ""
    asset_source_id: str = ""
    bus_refs: List[str] = field(default_factory=list)
    value: Optional[object] = None
    unit: str = ""
    reference_timestamp: str = ""
    reference_event_id: str = ""
    why: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "point_id": self.point_id,
            "kind": self.kind,
            "sub_kind": self.sub_kind,
            "feeder_id": self.feeder_id,
            "parameter": self.parameter,
            "asset_uid": self.asset_uid,
            "asset_source_id": self.asset_source_id,
            "bus_refs": list(self.bus_refs),
            "value": self.value,
            "unit": self.unit,
            "reference_timestamp": self.reference_timestamp,
            "reference_event_id": self.reference_event_id,
            "why": self.why,
        }


@dataclass
class TargetSelection:
    """Outcome of a target-selection attempt (always structured)."""

    status: str  # "SELECTED" | "FAILED"
    target: Optional[SelectedTarget] = None
    failure_reason: str = ""
    evaluated: int = 0
    candidates: List[SelectedTarget] = field(default_factory=list)
    seed: int = 42

    @classmethod
    def failed(cls, reason: str, candidates, evaluated: int, seed: int) -> "TargetSelection":
        return cls(status="FAILED", failure_reason=reason,
                   candidates=list(candidates), evaluated=evaluated, seed=seed)

    def as_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "failure_reason": self.failure_reason,
            "evaluated": self.evaluated,
            "candidate_count": len(self.candidates),
            "candidates": [c.as_dict() for c in self.candidates[:10]],
            "seed": self.seed,
        }


class SelectionRequirement:
    """What a compatible target must look like for one attack."""

    def __init__(
        self,
        kind: str = "measurement",
        sub_kinds: Sequence[str] = (),
        parameters: Sequence[str] = (),
        concepts: Sequence[str] = (),
        supported_only: bool = True,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        numeric: bool = False,
        prefer: Sequence[str] = (),
    ) -> None:
        self.kind = kind
        self.sub_kinds = tuple(sub_kinds)
        self.parameters = tuple(parameters)
        self.concepts = tuple(concepts)
        self.supported_only = supported_only
        self.min_value = min_value
        self.max_value = max_value
        self.numeric = numeric
        #: Adversary preference order for sub_kind, e.g. ("switch", "capacitor",
        #: "load") -- still deterministic, but earlier entries rank first.
        self.prefer = tuple(prefer)

    def priority(self, target: SelectedTarget) -> int:
        try:
            return self.prefer.index(target.sub_kind)
        except ValueError:
            return len(self.prefer)

    def matches(self, target: SelectedTarget) -> bool:
        if target.kind != self.kind:
            return False
        if self.sub_kinds and target.sub_kind not in self.sub_kinds:
            return False
        if self.parameters and target.parameter not in self.parameters:
            return False
        if self.concepts and target.sub_kind not in self.concepts:
            return False
        if self.numeric:
            value = target.value
            if not isinstance(value, (int, float)):
                return False
            if self.min_value is not None and value < self.min_value:
                return False
            if self.max_value is not None and value > self.max_value:
                return False
        return True


class TargetSelector:
    """Deterministically pick one compatible target from an inventory."""

    def __init__(self, inventory: Inventory, seed: int = 42) -> None:
        self.inventory = inventory
        self.seed = seed

    def select(self, requirement: SelectionRequirement) -> TargetSelection:
        candidate_targets = self._candidates(requirement)
        if not candidate_targets:
            return TargetSelection.failed(
                reason=self._no_match_reason(requirement),
                candidates=[],
                evaluated=len(candidate_targets),
                seed=self.seed,
            )
        index = self.seed % len(candidate_targets)
        chosen = candidate_targets[index]
        reason = (
            f"selected deterministically: {len(candidate_targets)} compatible "
            f"candidate(s), seed={self.seed} -> index {index}"
        )
        return TargetSelection(
            status="SELECTED",
            target=SelectedTarget(
                point_id=chosen.point_id,
                kind=chosen.kind,
                sub_kind=chosen.sub_kind,
                feeder_id=chosen.feeder_id,
                parameter=chosen.parameter,
                asset_uid=chosen.asset_uid,
                asset_source_id=chosen.asset_source_id,
                bus_refs=list(chosen.bus_refs),
                value=chosen.value,
                unit=chosen.unit,
                reference_timestamp=chosen.reference_timestamp,
                reference_event_id=chosen.reference_event_id,
                why=reason,
            ),
            candidates=candidate_targets,
            evaluated=len(candidate_targets),
            seed=self.seed,
        )

    def _candidates(self, requirement: SelectionRequirement) -> List[SelectedTarget]:
        raw: List[SelectedTarget] = []
        if requirement.kind == "measurement":
            for point in self.inventory.measurement_points:
                selected = point.selected()
                if requirement.matches(selected):
                    raw.append(selected)
        elif requirement.kind == "control":
            for point in self.inventory.control_points:
                if requirement.supported_only and not point.physically_supported:
                    continue
                selected = point.selected()
                if requirement.matches(selected):
                    raw.append(selected)
        elif requirement.kind == "scope":
            for target in self._scope_targets(requirement):
                if requirement.matches(target):
                    raw.append(target)

        if requirement.prefer:
            # Genuine cascade: restrict to the highest-priority concept tier
            # that actually has compatible candidates (prefer switch over
            # capacitor over load -- not just a sort hint drowned by the seed).
            present = [requirement.priority(t) for t in raw]
            top = min(present)
            raw = [t for t in raw if requirement.priority(t) == top]

        raw.sort(key=lambda t: t.point_id)
        return raw

    def _scope_targets(self, requirement: SelectionRequirement) -> List[SelectedTarget]:
        return [
            SelectedTarget(
                point_id=rtu_of(self.inventory.feeder_id),
                kind="scope",
                sub_kind="endpoint",
                feeder_id=self.inventory.feeder_id,
            )
        ]

    def _no_match_reason(self, requirement: SelectionRequirement) -> str:
        if requirement.kind == "measurement":
            available = self.inventory.measurement_points
            return (
                f"no compatible measurement target: need sub_kinds "
                f"{requirement.sub_kinds}; available kinds "
                f"{sorted({p.kind for p in available})}"
            )
        if requirement.kind == "control":
            return (
                f"no compatible control target: need concepts "
                f"{requirement.concepts or 'any'}, params "
                f"{requirement.parameters or 'any'}"
                f"{' physically supported' if requirement.supported_only else ''}; "
                f"available control points "
                f"{len(self.inventory.control_points)}"
            )
        return "no compatible scope target available"


# --------------------------------------------------------------------------- #
# target validation (F3)
# --------------------------------------------------------------------------- #


def validate_target(
    inventory: Inventory,
    target: Optional[SelectedTarget],
    feeder_id: str,
    requirement: Optional[SelectionRequirement] = None,
) -> Tuple[bool, str]:
    """Validate that ``target`` exists and is compatible in ``inventory``.

    Checks, in order:
    1. a target was actually selected;
    2. it belongs to this feeder context;
    3. it is discoverable in the inventory (exists);
    4. its kind is compatible with what the attack expects;
    5. its value is usable (finite numeric where the requirement asks for one).
    Returns ``(ok, reason)`` -- on failure, never falls through to target the
    point unchecked.
    """
    if target is None:
        return False, "no target was selected"
    if target.feeder_id and target.feeder_id != feeder_id:
        return False, (
            f"target {target.point_id!r} belongs to feeder {target.feeder_id!r}, "
            f"not the current context feeder {feeder_id!r}"
        )
    if target.kind == "measurement":
        point = inventory.measurement(target.point_id)
        if point is None:
            return False, f"target point {target.point_id!r} does not exist in this feeder's data"
        if point.feeder_id != feeder_id:
            return False, f"target point {target.point_id!r} belongs to another feeder"
        value = point.reference_value
        if value is None:
            return False, f"target point {target.point_id!r} has no usable value"
    elif target.kind == "control":
        point = inventory.control(target.point_id)
        if point is None:
            return False, f"target control point {target.point_id!r} does not exist in this feeder's model"
        if point.asset_uid.split("/", 1)[0] not in ("regulator", "switch", "capacitor", "load", "generator"):
            return False, f"target control point {target.point_id!r} is not an asset parameter"
    elif target.kind != "scope":
        return False, f"unsupported target kind {target.kind!r}"

    if requirement is not None and not requirement.matches(target):
        return False, f"target {target.point_id!r} is not compatible with the attack requirements"
    return True, "target is compatible"