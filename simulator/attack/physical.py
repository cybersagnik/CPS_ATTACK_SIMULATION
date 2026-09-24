"""Optional physical-simulation hook for attack-effect measurement.

The *network / event layer* is a pure consumer and never reruns OpenDSS -- that
rule belongs to ``simulator.network``.  The attack engine, however, must be
able to answer "what physical effect did this parameter/command produce?" for
scenarios such as *Parameter Modification* and *Unauthorized Command*.  This
module provides that capability: it rebuilds the real feeder's OpenDSS model
through the existing ``simulator.power.dss_builder`` and solves it twice -- once
at the normal baseline and once with the attack's parameter overrides applied --
then reports the voltage/power deltas.

Everything here is feeder-agnostic: overrides are keyed by the *model uid* of a
discovered controllable asset (``load/007``, ``capacitor/002``, ``switch/003``)
plus the exact parameter name from its :class:`ParameterSpec`.  No feeder id,
bus number or component name is referenced.

Scope & limitations (documented, not papered over):

* The current physical builder holds voltage regulators at neutral tap and does
  not emit regulator elements (no r-bus, a Phase D decision).  ``tap_position``
  is therefore **logically commandable but not physically computable** here;
  attempting it returns ``supported=False`` instead of a fabricated number.
* Solves run at the feeder's model-declared base load (real feeder data), or at
  profile-scaled loads when an ``assignment`` + ``profiles`` pair is supplied.
* A fresh circuit is built per solve; no state persists between runs, so state
  restoration is automatic (there is nothing to leak into a later run).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .events import AttackEventError

__all__ = [
    "PHYSICALLY_SUPPORTED",
    "PhysicalSimulationUnavailable",
    "SolveSnapshot",
    "PhysicalSimulationRunner",
    "effective_loads",
]

_PHASE_FROM_NODE = {1: "A", 2: "B", 3: "C"}
_PAIR_INDEX = {("1", "2"): 0, ("2", "3"): 1, ("3", "1"): 2}


#: ``(concept, parameter)`` pairs the physical runner can translate into an
#: OpenDSS edit, with a human reason.  Anything not listed here is still a
#: valid *logical* target but produces no physical effect.
PHYSICALLY_SUPPORTED: Dict[Tuple[str, str], str] = {
    ("load", "kw_multiplier"): "scales the load's active power at solve time",
    ("load", "kvar_multiplier"): "scales the load's reactive power at solve time",
    ("load", "enabled"): "connects / disconnects the load element",
    ("capacitor", "switched"): "connects / disconnects the capacitor element",
    ("capacitor", "kvar_multiplier"): "scales the capacitor kVAr rating",
    ("switch", "closed"): "connects / disconnects the switch line element",
}


class PhysicalSimulationUnavailable(RuntimeError):
    """Raised when ``opendssdirect`` (the power-flow engine) is not importable."""


def physically_supported(concept: str, parameter: str) -> bool:
    return (concept, parameter) in PHYSICALLY_SUPPORTED


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def effective_loads(model) -> Dict[str, Tuple[Dict[str, float], Dict[str, float]]]:
    """``uid -> (kw_per_phase, kvar_per_phase)`` from the model's declared loads."""
    out: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}
    for load in model.loads:
        out[load.uid] = (dict(load.kw_per_phase), dict(load.kvar_per_phase))
    return out


@dataclass
class SolveSnapshot:
    """Voltage/power footprint of one AC solve (feeder-agnostic)."""

    converged: bool = True
    vpu_by_bus: Dict[str, List[float]] = field(default_factory=dict)
    source_p_kw: float = 0.0
    source_q_kvar: float = 0.0

    @property
    def vpu_flat(self) -> List[float]:
        return [v for values in self.vpu_by_bus.values() for v in values]

    def vpu_of_bus(self, bus: str) -> Optional[float]:
        vals = self.vpu_by_bus.get(bus)
        return max(vals) if vals else None

    @property
    def vpu_min(self) -> Optional[float]:
        flat = self.vpu_flat
        return min(flat) if flat else None

    @property
    def vpu_max(self) -> Optional[float]:
        flat = self.vpu_flat
        return max(flat) if flat else None


@dataclass
class Override:
    """One applied attack override: an asset's parameter set to a value."""

    asset_uid: str
    concept: str
    parameter: str
    value: object


class PhysicalSimulationRunner:
    """Solve a real feeder model at baseline and with attack overrides applied.

    Feeder-agnostic: the model and the override maps are the only inputs.
    """

    def __init__(self, model) -> None:
        self.model = model
        self._loads = effective_loads(model)
        self._builder = None
        self._load_map: Dict[str, List[Tuple[str, str, float]]] = {}
        self._cap_elements: Dict[str, Dict[str, float]] = {}
        self._switch_elements: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------ #
    # circuit construction
    # ------------------------------------------------------------------ #
    def _build(self, dss) -> None:
        from simulator.power.dss_builder import DssBuilder

        builder = DssBuilder(self.model)
        self._builder = builder
        commands = list(builder.build_commands())
        self._load_map = builder.load_element_map()
        self._cap_elements = self._capacitor_elements()
        self._switch_elements = self._switch_lines()
        # DssBuilder only emits *closed* switches; emit the normally-open ones
        # ourselves (dissabled) so an attack may close them and the effect is
        # real, not silently ignored by OpenDSS.
        pairs = {
            frozenset(b)
            for b in self._transformer_pairs()
        }
        for sw in self.model.switches:
            if sw.closed:
                continue
            if frozenset((sw.bus1, sw.bus2)) in pairs:
                continue
            name = "SW_" + _safe(sw.source_id)
            commands.append(
                f"New Line.{name} Phases=3 Bus1={sw.bus1} Bus2={sw.bus2}"
                " r1=1e-3 r0=1e-3 x1=0 x0=0 c1=0 c0=0 Length=0.001 Units=mi"
                " enabled=False"
            )
        dss.Text.Commands(commands)

    def _transformer_pairs(self) -> List[Tuple[str, str]]:
        """Transformer terminal bus pairs (switches there are bypassed)."""
        pairs: List[Tuple[str, str]] = []
        for t in self.model.transformers:
            buses = tuple(str(b).split(".")[0] for b in t.buses if b)
            if len(buses) >= 2:
                pairs.append((buses[0], buses[1]))
        return pairs

    def _capacitor_elements(self) -> Dict[str, Dict[str, float]]:
        """``uid -> {element_name: base_kvar}`` mirroring DssBuilder._capacitors."""
        out: Dict[str, Dict[str, float]] = {}
        for cap in self.model.capacitors:
            phases = [p for p in ("A", "B", "C") if cap.kvar_per_phase.get(p, 0.0) > 0]
            base = f"C_{_safe(cap.source_id)}"
            if len(phases) == 3:
                out[cap.uid] = {base: sum(cap.kvar_per_phase.values())}
            else:
                out[cap.uid] = {
                    f"{base}_{phase}": cap.kvar_per_phase[phase] for phase in phases
                }
        return out

    def _switch_lines(self) -> Dict[str, List[Tuple[str, bool]]]:
        """``uid -> [(element_name, is_closed)]`` matching DssBuilder._switches."""
        out: Dict[str, List[Tuple[str, bool]]] = {}
        for sw in self.model.switches:
            out[sw.uid] = [("SW_" + _safe(sw.source_id), sw.closed)]
        return out

    # ------------------------------------------------------------------ #
    # snapshot / solve
    # ------------------------------------------------------------------ #
    def _snapshot(self, dss) -> SolveSnapshot:
        snap = SolveSnapshot()
        snap.converged = bool(dss.Solution.Converged())
        tp = dss.Circuit.TotalPower()
        snap.source_p_kw = -float(tp[0])
        snap.source_q_kvar = -float(tp[1])

        skip = {"SYSsource", "syssource"}
        for name in dss.Circuit.AllBusNames():
            if name in skip:
                continue
            dss.Circuit.SetActiveBus(name)
            volts = dss.Bus.Voltages()
            if not volts:
                continue
            n = len(volts) // 2
            nodes = [complex(volts[2 * i], volts[2 * i + 1]) for i in range(n)]
            pairs: List[float] = []
            for p1, p2 in (("1", "2"), ("2", "3"), ("3", "1")):
                i1, i2 = int(p1) - 1, int(p2) - 1
                if i1 < n and i2 < n:
                    pairs.append(abs(nodes[i1] - nodes[i2]))
            if pairs and self._bus_base_kv(name):
                base = self._bus_base_kv(name) * 1000.0
                snap.vpu_by_bus[name] = [v / base for v in pairs]
        return snap

    def _bus_base_kv(self, bus: str) -> Optional[float]:
        for tgroup in self.model.transformers:
            for b in tgroup.buses:
                if str(b).split(".")[0] == bus:
                    return float(self.model.nominal_kv)
        return float(self.model.nominal_kv)

    # ------------------------------------------------------------------ #
    # override translation -> OpenDSS edit commands
    # ------------------------------------------------------------------ #
    def _apply_overrides(self, dss, overrides: Dict[str, Dict[str, object]]) -> List[str]:
        """Return the DSS edit commands that realise ``overrides``.

        ``overrides`` maps an asset uid (``load/007``) to ``{param: value}``.
        Raises :class:`AttackEventError` for overrides the physical model
        cannot represent (no fabricated numbers; fail with a reason).
        """
        edits: List[str] = []
        kw: Dict[str, Dict[str, float]] = {}
        kvar: Dict[str, Dict[str, float]] = {}
        for uid in self._loads:
            kw[uid] = dict(self._loads[uid][0])
            kvar[uid] = dict(self._loads[uid][1])

        for uid, params in overrides.items():
            concept = uid.split("/", 1)[0] if "/" in uid else uid
            if not params:
                continue
            for param, value in params.items():
                key = (concept or "", param)
                if not physically_supported(key[0], key[1]):
                    raise AttackEventError(
                        f"parameter {uid}.{param} is not physically supported "
                        f"(supported: {sorted(PHYSICALLY_SUPPORTED)})"
                    )
                if concept == "load":
                    edits += self._load_edits(uid, param, value, kw, kvar)
                elif concept == "capacitor":
                    edits += self._capacitor_edits(uid, param, value)
                elif concept == "switch":
                    edits += self._switch_edits(uid, param, value)
                else:
                    raise AttackEventError(f"concept {concept!r} has no physical override")
        edits += self._builder.update_load_commands(kw, kvar)
        return edits

    def _load_edits(self, uid, param, value, kw, kvar) -> List[str]:
        elements = self._load_map.get(uid, [])
        if not elements:
            return []
        if param == "kw_multiplier":
            for phase, base_kw in self._loads[uid][0].items():
                kw[uid][phase] = base_kw * float(value)
            return []
        if param == "kvar_multiplier":
            for phase, base_kvar in self._loads[uid][1].items():
                kvar[uid][phase] = base_kvar * float(value)
            return []
        if param == "enabled":
            return [f"Edit Load.{name} enabled={bool(value)}" for name, _, _ in elements]
        raise AttackEventError(f"load parameter {param!r} unsupported")

    def _capacitor_edits(self, uid, param, value) -> List[str]:
        elements = list(self._cap_elements.get(uid, {}).items())
        edits = []
        if param == "switched":
            state = "True" if bool(value) else "False"
            for name, _ in elements:
                edits.append(f"Edit Capacitor.{name} enabled={state}")
        elif param == "kvar_multiplier":
            for name, base in elements:
                edits.append(f"Edit Capacitor.{name} kVAR={base * float(value):.6g}")
        else:
            raise AttackEventError(f"capacitor parameter {param!r} unsupported")
        return edits

    def _switch_edits(self, uid, param, value) -> List[str]:
        if param != "closed":
            raise AttackEventError(f"switch parameter {param!r} unsupported")
        names = self._switch_elements.get(uid, [])
        return [
            f"Edit Line.{name} enabled={bool(value)}"
            for name, _ in names
        ]

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def solve(self, overrides: Optional[Dict[str, Dict[str, object]]] = None) -> SolveSnapshot:
        """Solve the feeder once with (optionally) attack overrides applied."""
        try:
            import opendssdirect as dss
        except Exception as exc:  # pragma: no cover - env dependent
            raise PhysicalSimulationUnavailable(
                "opendssdirect is not installed in this environment"
            ) from exc

        self._build(dss)
        if overrides:
            edits = self._apply_overrides(dss, overrides)
            if edits:
                dss.Text.Commands(edits)
        dss.Solution.Solve()
        return self._snapshot(dss)


def effect_snapshot(runner: PhysicalSimulationRunner,
                    overrides: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    """Compute a baseline / affected footprint pair for ``overrides``.

    Returns a plain dict (no fabricated numbers): ``baseline``, ``affected``
    and ``deltas`` (``vpu_min``, ``vpu_max``, ``source_p_kw`` under the attack
    minus their baseline values).
    """
    base = runner.solve()
    affected = runner.solve(overrides)
    def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None:
            return None
        return b - a
    return {
        "baseline": {
            "vpu_min": base.vpu_min,
            "vpu_max": base.vpu_max,
            "source_p_kw": base.source_p_kw,
            "source_q_kvar": base.source_q_kvar,
            "converged": base.converged,
        },
        "affected": {
            "vpu_min": affected.vpu_min,
            "vpu_max": affected.vpu_max,
            "source_p_kw": affected.source_p_kw,
            "source_q_kvar": affected.source_q_kvar,
            "converged": affected.converged,
        },
        "deltas": {
            "vpu_min": _delta(base.vpu_min, affected.vpu_min),
            "vpu_max": _delta(base.vpu_max, affected.vpu_max),
            "source_p_kw": _delta(base.source_p_kw, affected.source_p_kw),
        },
    }