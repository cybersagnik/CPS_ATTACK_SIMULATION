"""Feeder-agnostic common grid representation.

This module is the *only* object graph the attack engine, scenario runner and
dataset writers are allowed to see.  It deliberately contains no feeder-specific
attack logic: component types are generic concepts ("regulator", "switch",
"measurement", ...) while the original feeder identifiers are preserved purely
as tracing metadata in each component's ``source_id`` field.

Never write attack code that looks up ``source_id == "IEEE123_Regulator_4"``.
Instead ask the model for a concept::

    model.regulators()        ->  "all voltage regulators"
    model.switches()          ->  "all switches"
    model.controllable_assets() -> every asset with a tunable parameter

Feeder adapters populate these containers; the pipeline never parses raw
feeder files directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

__all__ = [
    "ParameterSpec",
    "Bus",
    "Line",
    "LineConfiguration",
    "Transformer",
    "Regulator",
    "Capacitor",
    "Switch",
    "Load",
    "Generator",
    "Measurement",
    "FeederModel",
    "REGULATOR_CONTROL",
    "SWITCH_CONTROL",
    "CAPACITOR_CONTROL",
    "LOAD_CONTROL",
    "GENERATOR_CONTROL",
]

# --------------------------------------------------------------------------- #
# Controllable parameter specifications (shared vocabulary for the attack engine)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParameterSpec:
    """Description of one tunable parameter exposed on a component."""

    name: str
    kind: str  # "int" | "float" | "bool" | "str"
    unit: str = ""
    description: str = ""
    min: Optional[float] = None
    max: Optional[float] = None


REGULATOR_CONTROL: Tuple[ParameterSpec, ...] = (
    ParameterSpec("tap_position", "int", "steps", "Regulator tap position", -16, 16),
    ParameterSpec("bandwidth_volts", "float", "V", "Voltage regulator bandwidth", 0.0, None),
    ParameterSpec("voltage_level", "float", "V", "Regulated center voltage", 110.0, 130.0),
    ParameterSpec("enabled", "bool", "", "Regulator control enabled"),
)

SWITCH_CONTROL: Tuple[ParameterSpec, ...] = (
    ParameterSpec("closed", "bool", "", "Switch closed (True) or open (False)"),
)

CAPACITOR_CONTROL: Tuple[ParameterSpec, ...] = (
    ParameterSpec("switched", "bool", "", "Capacitor bank switched in"),
    ParameterSpec("kvar_multiplier", "float", "pu", "Capacitor kVAr multiplier", 0.0, None),
)

LOAD_CONTROL: Tuple[ParameterSpec, ...] = (
    ParameterSpec("kw_multiplier", "float", "pu", "Active power multiplier", 0.0, None),
    ParameterSpec("kvar_multiplier", "float", "pu", "Reactive power multiplier", 0.0, None),
    ParameterSpec("enabled", "bool", "", "Load connected"),
)

GENERATOR_CONTROL: Tuple[ParameterSpec, ...] = (
    ParameterSpec("kw_multiplier", "float", "pu", "Generator kW multiplier", 0.0, None),
    ParameterSpec("kvar_multiplier", "float", "pu", "Generator kVAr multiplier", 0.0, None),
    ParameterSpec("enabled", "bool", "", "Generator online"),
)

# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


@dataclass
class Bus:
    """A node / bus in the feeder network."""

    concept: str = "bus"
    uid: str = ""
    source_id: str = ""  # original feeder node id, e.g. "701"
    phases: List[str] = field(default_factory=list)
    nominal_kv: float = 0.0


@dataclass
class Line:
    """A distribution line segment between two buses."""

    concept: str = "line"
    uid: str = ""
    source_id: str = ""  # derived feeder id, e.g. "701-702"
    bus1: str = ""
    bus2: str = ""
    phases: List[str] = field(default_factory=list)
    length_ft: float = 0.0
    config_source_id: str = ""  # feeder-local configuration id, e.g. "722"


@dataclass
class LineConfiguration:
    """Underground / overhead cable configuration referenced by lines."""

    concept: str = "line_configuration"
    uid: str = ""
    source_id: str = ""  # feeder-local config id, e.g. "721"
    kind: str = ""  # "underground" | "overhead"
    phases: str = ""
    conductor: str = ""
    spacing_id: str = ""


@dataclass
class Transformer:
    """Two- or multi-winding transformer (substation or feeder level)."""

    concept: str = "transformer"
    uid: str = ""
    source_id: str = ""  # feeder-local name, e.g. "Substation", "XFM-1"
    buses: Tuple[str, str] = ("", "")  # (high side, low side) feeder-local node refs
    kva: float = 0.0
    kv_high: float = 0.0
    kv_low: float = 0.0
    connection_high: str = ""
    connection_low: str = ""
    r_pct: float = 0.0
    x_pct: float = 0.0


@dataclass
class Regulator:
    """Voltage regulator bank (controllable tap)."""

    concept: str = "regulator"
    uid: str = ""
    source_id: str = ""  # feeder-local regulator id, e.g. "1"
    line_segment: str = ""  # feeder-local "from-to" segment the regulator sits on
    location: str = ""  # feeder-local node id
    phases: List[str] = field(default_factory=list)
    connection: str = ""
    bandwidth_volts: float = 0.0
    pt_ratio: float = 0.0
    ct_rating: float = 0.0
    comp_r: float = 0.0
    comp_x: float = 0.0
    voltage_level: float = 0.0
    control: Tuple[ParameterSpec, ...] = REGULATOR_CONTROL


@dataclass
class Capacitor:
    """Shunt capacitor bank (controllable switched state)."""

    concept: str = "capacitor"
    uid: str = ""
    source_id: str = ""
    bus: str = ""
    phases: List[str] = field(default_factory=list)
    kvar_per_phase: Dict[str, float] = field(default_factory=dict)
    control: Tuple[ParameterSpec, ...] = CAPACITOR_CONTROL


@dataclass
class Switch:
    """Sectionalizing switch (controllable open/closed state)."""

    concept: str = "switch"
    uid: str = ""
    source_id: str = ""
    bus1: str = ""
    bus2: str = ""
    normal_state: str = ""  # feeder-local "open"/"closed"
    closed: bool = True
    control: Tuple[ParameterSpec, ...] = SWITCH_CONTROL


@dataclass
class Load:
    """A load attached to a bus (constant PQ / I / Z)."""

    concept: str = "load"
    uid: str = ""
    source_id: str = ""  # feeder-local load node id
    bus: str = ""
    phases: List[str] = field(default_factory=list)
    load_model: str = ""  # "PQ" | "I" | "Z"
    connection: str = ""  # "wye" | "delta"
    kw_per_phase: Dict[str, float] = field(default_factory=dict)
    kvar_per_phase: Dict[str, float] = field(default_factory=dict)
    control: Tuple[ParameterSpec, ...] = LOAD_CONTROL

    @property
    def total_kw(self) -> float:
        return sum(self.kw_per_phase.values())

    @property
    def total_kvar(self) -> float:
        return sum(self.kvar_per_phase.values())


@dataclass
class Generator:
    """Generator / PV source (kept for feeders that have one)."""

    concept: str = "generator"
    uid: str = ""
    source_id: str = ""
    bus: str = ""
    phases: List[str] = field(default_factory=list)
    kw: float = 0.0
    kvar: float = 0.0
    connection: str = ""
    control: Tuple[ParameterSpec, ...] = GENERATOR_CONTROL


@dataclass
class Measurement:
    """A measurement / telemetry point.

    ``derived=True`` means the adapter synthesised the point (IEEE test feeders
    ship no SCADA metadata); the network simulator decides which points are
    *reported* so false-measurement attacks have a feeder-agnostic surface.
    """

    concept: str = "measurement"
    uid: str = ""
    source_id: str = ""
    kind: str = ""  # "voltage" | "current" | "power" | "tap"
    bus: str = ""
    phases: List[str] = field(default_factory=list)
    unit: str = ""
    derived: bool = True


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class FeederModel:
    """The common grid model every downstream stage operates on."""

    id: str
    name: str
    nominal_kv: float
    frequency_hz: float = 60.0
    base_phases: Tuple[str, ...] = ("A", "B", "C")

    buses: List[Bus] = field(default_factory=list)
    lines: List[Line] = field(default_factory=list)
    configurations: List[LineConfiguration] = field(default_factory=list)
    transformers: List[Transformer] = field(default_factory=list)
    regulators: List[Regulator] = field(default_factory=list)
    capacitors: List[Capacitor] = field(default_factory=list)
    switches: List[Switch] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    generators: List[Generator] = field(default_factory=list)
    measurements: List[Measurement] = field(default_factory=list)

    warnings: List[str] = field(default_factory=list)
    _bus_index: Dict[str, Bus] = field(default_factory=dict, repr=False, compare=False)

    # -- indexing ---------------------------------------------------------- #
    def build_index(self) -> "FeederModel":
        self._bus_index = {b.source_id: b for b in self.buses}
        return self

    def bus(self, source_id: str) -> Bus:
        return self._bus_index[source_id]

    # -- concept accessors (what the attack engine may use) ----------------- #
    def all_components(self) -> Iterator:
        """Yield every component instance (tagged with ``concept``)."""
        groups = (
            self.buses,
            self.lines,
            self.configurations,
            self.transformers,
            self.regulators,
            self.capacitors,
            self.switches,
            self.loads,
            self.generators,
            self.measurements,
        )
        for group in groups:
            yield from group

    def supported_device_types(self) -> set:
        """Concept names that actually occur in this feeder (non-empty)."""
        return {c.concept for c in self.all_components()}

    def controllable_assets(self) -> Iterator:
        """Yield assets that expose tunable parameters (attack surface)."""
        for asset in (
            *self.regulators,
            *self.switches,
            *self.capacitors,
            *self.loads,
            *self.generators,
        ):
            if getattr(asset, "control", None):
                yield asset

    def controllable_parameters(self) -> Iterator:
        """Yield ``(asset, ParameterSpec)`` pairs for every tunable parameter."""
        for asset in self.controllable_assets():
            for spec in asset.control:
                yield asset, spec

    # -- profile-assignment targets (feeder-agnostic id/bus lookup) ---------- #
    def _target_bus_and_connection(self, component: object, concept: str) -> Tuple[object, object]:
        """Resolve the bus reference(s) and connection descriptor for a component."""
        if concept == "switch":
            return (component.bus1, component.bus2), component.normal_state
        if concept == "regulator":
            return component.location, component.connection
        if concept == "transformer":
            return tuple(component.buses), (
                component.connection_high,
                component.connection_low,
            )
        if concept == "line":
            return (component.bus1, component.bus2), ""
        if concept == "bus":
            return component.source_id, ""
        return getattr(component, "bus", component.source_id), getattr(
            component, "connection", ""
        )

    def component_targets(self, concept: str) -> Dict[str, Dict[str, Dict[str, object]]]:
        """``feeder_id -> component id -> {bus_id, source_id, connection, ...}``.

        Generic lookup for any supported concept (``"load"``, ``"generator"``,
        ``"switch"``, ``"regulator"``, ``"transformer"``, ...).  ``id`` is the
        model's generic uid (``{concept}/NNN``); the original feeder id is
        preserved in ``source_id``.  Works identically for any loaded feeder.
        """
        groups = {
            "bus": self.buses,
            "line": self.lines,
            "line_configuration": self.configurations,
            "transformer": self.transformers,
            "regulator": self.regulators,
            "capacitor": self.capacitors,
            "switch": self.switches,
            "load": self.loads,
            "generator": self.generators,
            "measurement": self.measurements,
        }
        mapping: Dict[str, Dict[str, Dict[str, object]]] = {self.id: {}}
        for component in groups.get(concept, ()):
            bus_id, connection = self._target_bus_and_connection(component, concept)
            mapping[self.id][component.uid] = {
                "id": component.uid,
                "source_id": component.source_id,
                "bus_id": bus_id,
                "connection": connection,
            }
        return mapping

    def get_load_targets(self) -> Dict[str, Dict[str, Dict[str, object]]]:
        """``feeder_id -> load_id -> {bus_id, source_id, connection, ...}``.

        Every load of this feeder under one common shape, e.g.::

            {"ieee37": {"load/001": {"source_id": "701", "bus_id": "701",
                                     "connection": "delta", ...}, ...}}
        """
        return self.component_targets("load")

    def get_generator_targets(self) -> Dict[str, Dict[str, Dict[str, object]]]:
        """``feeder_id -> generator_id -> {bus_id, source_id, connection, ...}``.

        Same shape as :meth:`get_load_targets`; empty per feeder until a real
        source ships generator/PV data.
        """
        return self.component_targets("generator")

    def device_counts(self) -> Dict[str, int]:
        return {
            "buses": len(self.buses),
            "lines": len(self.lines),
            "transformers": len(self.transformers),
            "regulators": len(self.regulators),
            "capacitors": len(self.capacitors),
            "switches": len(self.switches),
            "loads": len(self.loads),
            "generators": len(self.generators),
            "measurements": len(self.measurements),
        }