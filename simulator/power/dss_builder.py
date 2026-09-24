"""Translate a :class:`~simulator.power.common_model.FeederModel` into an
OpenDSS power-flow model (feeder-agnostic).

No attack logic and no hard-coded feeder ids live here: the translation is
driven purely by the component lists inside the common model plus two small
*physical* data tables that the IEEE source workbooks do not carry
machine-readably:

* ``data/line_impedance_ohm_mi.csv``  -- per-mile phase impedance matrices of
  the configuration codes used by the loadable IEEE test feeders (sourced from
  the published OpenDSS ``IEEELineCodes.DSS`` data, corrected 2010-09-16).
* ``data/dss_bus_overrides.csv``      -- terminal-bus row for the IEEE-123
  XFM-1 load transformer, whose source workbook has no link row (the adapter
  therefore stores empty ``buses``); the row restores the documented 4.16 kV
  bus ``61`` / 0.48 kV bus ``610`` termination.

Modelled decisions (documented, reproducible):

* Voltage regulators are held at their nominal tap, which for the classic
  test feeders is a zero-impedance pass-through; the common model's topology
  contains no ``<bus>r`` split terminals, so the regulator elements are not
  emitted.  This keeps normal-scenario runs identical to the topology that the
  CGM actually describes.
* ``kw``/``kvar`` recorded per phase on every load are handed straight to the
  solver; OpenDSS ``Model`` codes map ``PQ -> 1``, ``I -> 3``, ``Z -> 2``,
  ``PR -> 1`` and numeric codes pass through verbatim.
* Line lengths stored by the adapter are 1/1000-mile units for these
  workbooks; they are converted to miles here (``Units=mi`` everywhere).
"""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .common_model import FeederModel, Load

__all__ = ["DssBuilder", "load_impedance_table", "load_bus_overrides"]

_DATA_DIR = Path(__file__).resolve().parent / "data"

_PHASE_INDEX = {"A": 1, "B": 2, "C": 3}
_PAIR_FOR_PHASE = {"A": (1, 2), "B": (2, 3), "C": (3, 1)}

# OpenDSS Load.Model codes: 1 = constant P+jQ, 2 = constant Z, 3 = constant I.
_DSS_MODEL = {
    "PQ": 1,
    "I": 3,
    "Z": 2,
    "PR": 1,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "": 1,
}


def load_impedance_table(path: Optional[Path] = None) -> Dict[Tuple[str, str], Dict]:
    """``{(feeder_id, config_id): dict}`` with ``nphases`` and the ``r``/``x``
    lower-triangle lists (ohms per mile)."""
    path = path or (_DATA_DIR / "line_impedance_ohm_mi.csv")
    table: Dict[Tuple[str, str], Dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            nphases = int(row["nphases"])
            r = [float(row[f"r{j}{i}"]) for j in range(1, nphases + 1)
             for i in range(1, j + 1)]
            x = [float(row[f"x{j}{i}"]) for j in range(1, nphases + 1)
                 for i in range(1, j + 1)]
            table[(row["feeder_id"], row["config_id"])] = {
                "nphases": nphases,
                "r": r,
                "x": x,
            }
    return table


def load_bus_overrides(path: Optional[Path] = None) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """``{(feeder_id, source_id): (bus1, bus2)}`` terminal-bus overrides."""
    path = path or (_DATA_DIR / "dss_bus_overrides.csv")
    overrides: Dict[Tuple[str, str], Tuple[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            overrides[(row["feeder_id"], row["source_id"])] = (row["bus1"], row["bus2"])
    return overrides


def _safe_name(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", uid)


def _nodes(bus: str, phases: Sequence[str]) -> str:
    """OpenDSS bus reference for the given bus with explicit phase nodes."""
    present = [p for p in ("A", "B", "C") if p in phases]
    if len(present) == 3:
        return bus
    return bus + "." + ".".join(str(_PHASE_INDEX[p]) for p in present)


def _matrix_block(tag: str, values: Sequence[float]) -> str:
    """Format a lower-triangle symmetric matrix for a DSS LineCode.  OpenDSS
    reads the declared lower triangle only: each ``|`` separates a matrix row
    (row ``i`` carries ``i`` values), matching ``IEEELineCodes.DSS``."""
    values = list(values)
    rows: List[str] = []
    index = 0
    for i in range(1, 4):
        if index >= len(values):
            break
        row = [f"{values[index + k]:.9g}" for k in range(i) if index + k < len(values)]
        rows.append(" ".join(row))
        index += i
    return f" {tag}=[" + " | ".join(rows) + "]"


def _conn(token: str) -> str:
    return "Delta" if "D" in str(token).upper() and "W" not in str(token).upper() else "Wye"


class DssBuilder:
    """Builds the DSS command script for one feeder's normal (attack-free)
    power-flow model."""

    def __init__(self, model: FeederModel) -> None:
        self.model = model
        self._impedance = load_impedance_table()
        self._overrides = load_bus_overrides()
        self.issues: List[str] = []
        self._load_element_map: Dict[str, List[Tuple[str, str, float]]] = {}

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def build_commands(self) -> List[str]:
        """Return the full DSS script (network + base loads at model values)."""
        cmd: List[str] = []
        cmd.append("Clear")
        self._circuit(cmd)
        self._transformers(cmd)
        self._line_codes(cmd)
        self._lines(cmd)
        self._switches(cmd)
        self._capacitors(cmd)
        for load in self.model.loads:
            self._load(cmd, load, load.kw_per_phase, load.kvar_per_phase)
        self._voltage_bases(cmd)
        return cmd

    def load_element_map(self) -> Dict[str, List[Tuple[str, str, float]]]:
        """``uid -> [(dss element name, phase, phase_kw)]`` used to hot-swap
        load powers between timesteps without recompiling the network."""
        return self._load_element_map

    def update_load_commands(self, kw: Dict[str, Dict[str, float]],
                             kvar: Dict[str, Dict[str, float]]) -> List[str]:
        """``Edit`` commands that set the given per-uid per-phase powers."""
        out: List[str] = []
        for uid, elements in self._load_element_map.items():
            if elements and elements[0][0].startswith("full3_"):
                out.append(
                    f"Edit Load.{elements[0][0]} kw={sum(kw.get(uid, {}).values()):.6g}"
                    f" kvar={sum(kvar.get(uid, {}).values()):.6g}"
                )
                continue
            for name, phase, _ in elements:
                out.append(
                    f"Edit Load.{name} kw={kw.get(uid, {}).get(phase, 0.0):.6g}"
                    f" kvar={kvar.get(uid, {}).get(phase, 0.0):.6g}"
                )
        return out

    # ------------------------------------------------------------------ #
    # script sections
    # ------------------------------------------------------------------ #
    def _circuit(self, cmd: List[str]) -> None:
        sub = self._substation_transformer()
        kv_high = sub.kv_high if sub else self.model.nominal_kv
        cmd.append(
            f"New Circuit.{_safe_name(self.model.id)} basekv={kv_high:.6g}"
            " Bus1=SYSsource pu=1.0"
        )
        cmd.append("Edit Vsource.source R1=0 X1=0.0001 R0=0 X0=0.0001")
        cmd.append("Set MaxIterations=100")
        cmd.append("Set ControlMode=OFF")

    def _substation_transformer(self) -> Optional[object]:
        for t in self.model.transformers:
            if str(t.buses[0]).endswith(".hv") or t.source_id.upper().startswith("SUBSTATION"):
                return t
        return None

    def _transformers(self, cmd: List[str]) -> None:
        endpoints = {bus for line in self.model.lines for bus in (line.bus1, line.bus2)}
        for t in self.model.transformers:
            name = f"T_{_safe_name(t.source_id)}"
            buses = self._resolved_buses(t)
            if self._substation_transformer() is t:
                buses = ("SYSsource", str(buses[1]).removesuffix(".hv"))
            else:
                buses = self._terminal_order(t, buses, endpoints, float(self.model.nominal_kv))
            if not all(buses):
                self.issues.append(
                    f"transformer {t.source_id!r}: no terminal buses available; skipped"
                )
                continue
            cmd.append(
                f"New Transformer.{name} Phases=3 Windings=2"
                f" Buses=({buses[0]} {buses[1]})"
                f" Conns=({_conn(t.connection_high)} {_conn(t.connection_low)})"
                f" KVs=({t.kv_high:.6g} {t.kv_low:.6g})"
                f" kvas=({t.kva:.6g} {t.kva:.6g})"
                f" XHL={t.x_pct:.6g} %LoadLoss={t.r_pct:.6g}"
            )

    @staticmethod
    def _terminal_order(t, buses: Tuple[str, str], endpoints: set,
                        feeder_kv: float) -> Tuple[str, str]:
        """The CGM transformer terminal ordering is not meaningful; resolve the
        high- (feeder-level) and low-voltage buses from the declared KVs.  The
        winding whose KV matches the feeder nominal is the main-network side."""
        if t.kv_high == t.kv_low:
            return buses
        d_hi = abs(float(t.kv_high) - feeder_kv)
        d_lo = abs(float(t.kv_low) - feeder_kv)
        candidates = [b for b in buses if b in endpoints]
        if d_hi <= d_lo:
            hi = candidates[0] if candidates else buses[0]
            lo = buses[0] if buses[1] == hi else buses[1]
        else:
            lo = candidates[0] if candidates else buses[1]
            hi = buses[0] if buses[1] == lo else buses[1]
        return (hi, lo)

    def _resolved_buses(self, t) -> Tuple[str, str]:
        if all(t.buses):
            return t.buses
        override = self._overrides.get((self.model.id, t.source_id))
        if override is None:
            return ("", "")
        self.issues.append(
            f"transformer {t.source_id!r}: restored terminal buses from the "
            f"physical-data override table -> {override}"
        )
        return override

    def _line_codes(self, cmd: List[str]) -> None:
        used = sorted({line.config_source_id for line in self.model.lines})
        for config_id in used:
            spec = self._impedance.get((self.model.id, config_id))
            if spec is None:
                self.issues.append(
                    f"line config {config_id!r}: no impedance data for feeder "
                    f"{self.model.id!r}; lines using it will be skipped"
                )
                continue
            cmd.append(
                "New LineCode.LC" + config_id
                + f" nphases={spec['nphases']} Units=mi"
                + _matrix_block("Rmatrix", spec["r"])
                + _matrix_block("Xmatrix", spec["x"])
                + " NormAmps=400"
            )

    def _lines(self, cmd: List[str]) -> None:
        skip = {frozenset(b) for b in self._transformer_pairs()}
        for line in self.model.lines:
            spec = self._impedance.get((self.model.id, line.config_source_id))
            if spec is None:
                self.issues.append(
                    f"line {line.source_id!r}: config {line.config_source_id!r} "
                    "has no impedance data; skipped"
                )
                continue
            if frozenset((line.bus1, line.bus2)) in skip:
                self.issues.append(
                    f"line {line.source_id!r}: spans a transformer terminal pair; "
                    "the transformer carries the connection"
                )
                continue
            length_mi = line.length_ft / 1000.0
            cmd.append(
                f"New Line.{_safe_name('L_' + line.source_id)}"
                f" Phases={spec['nphases']}"
                f" Bus1={_nodes(line.bus1, line.phases)}"
                f" Bus2={_nodes(line.bus2, line.phases)}"
                f" LineCode=LC{line.config_source_id}"
                f" Length={length_mi:.9g} Units=mi"
            )

    def _transformer_pairs(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        for t in self.model.transformers:
            if self._substation_transformer() is t:
                pairs.append(("SYSsource", str(t.buses[1]).removesuffix(".hv")))
            else:
                pairs.append(self._resolved_buses(t))
        return pairs

    def _switches(self, cmd: List[str]) -> None:
        pairs = {frozenset(b) for b in self._transformer_pairs()}
        for sw in self.model.switches:
            if not sw.closed:
                continue
            if frozenset((sw.bus1, sw.bus2)) in pairs:
                self.issues.append(
                    f"switch {sw.source_id!r}: coincides with a transformer "
                    "terminal pair; the transformer carries the connection"
                )
                continue
            cmd.append(
                f"New Line.{_safe_name('SW_' + sw.source_id)} Phases=3"
                f" Bus1={sw.bus1} Bus2={sw.bus2}"
                " r1=1e-3 r0=1e-3 x1=0 x0=0 c1=0 c0=0 Length=0.001 Units=mi"
            )

    def _capacitors(self, cmd: List[str]) -> None:
        for cap in self.model.capacitors:
            name = f"C_{_safe_name(cap.source_id)}"
            phases = [p for p in ("A", "B", "C") if cap.kvar_per_phase.get(p, 0.0) > 0]
            if len(phases) == 3:
                cmd.append(
                    f"New Capacitor.{name} Bus1={cap.bus} Phases=3"
                    f" kVAR={sum(cap.kvar_per_phase.values()):.6g}"
                    f" kV={self.model.nominal_kv:.6g}"
                )
                continue
            for phase in phases:
                cmd.append(
                    f"New Capacitor.{name}_{phase} Bus1={cap.bus}.{_PHASE_INDEX[phase]}"
                    f" Phases=1 kVAR={cap.kvar_per_phase[phase]:.6g}"
                    f" kV={self.model.nominal_kv / math.sqrt(3):.6g}"
                )

    def _load(self, cmd: List[str], load: Load,
              kw: Dict[str, float], kvar: Dict[str, float]) -> None:
        name_base = f"L_{_safe_name(load.uid)}"
        elements: List[Tuple[str, str, float]] = []
        active = [p for p in ("A", "B", "C") if kw.get(p, 0.0) or kvar.get(p, 0.0)]
        model_code = _DSS_MODEL.get(load.load_model, 1)
        kv_ln = self.model.nominal_kv / math.sqrt(3)
        kv_ll = self.model.nominal_kv
        if not active:
            self._load_element_map[load.uid] = elements
            return

        if load.connection == "delta":
            for phase in active:
                p1, p2 = _PAIR_FOR_PHASE[phase]
                el = f"{name_base}_{phase}"
                cmd.append(
                    f"New Load.{el} Phases=1 Bus1={load.bus}.{p1}.{p2}"
                    f" Conn=Delta kV={kv_ll:.6g}"
                    f" kW={kw.get(phase, 0.0):.6g} kvar={kvar.get(phase, 0.0):.6g}"
                    f" Model={model_code}"
                )
                elements.append((el, phase, kw.get(phase, 0.0)))
            self._load_element_map[load.uid] = elements
            return

        if active == ["A", "B", "C"]:
            vals = [kw[p] for p in active]
            if all(kw[p] >= 0 for p in active) and max(vals) - min(vals) <= 1e-9:
                el = f"full3_{name_base}"
                cmd.append(
                    f"New Load.{el} Phases=3 Conn=Wye kV={kv_ll:.6g}"
                    f" kW={sum(kw.values()):.6g} kvar={sum(kvar.values()):.6g}"
                    f" Model={model_code}"
                )
                self._load_element_map[load.uid] = [(el, "A", sum(kw.values()))]
                return

        for phase in active:
            el = f"{name_base}_{phase}"
            cmd.append(
                f"New Load.{el} Phases=1 Bus1={load.bus}.{_PHASE_INDEX[phase]}"
                f" Conn=Wye kV={kv_ln:.6g}"
                f" kW={kw.get(phase, 0.0):.6g} kvar={kvar.get(phase, 0.0):.6g}"
                f" Model={model_code}"
            )
            elements.append((el, phase, kw.get(phase, 0.0)))
        self._load_element_map[load.uid] = elements

    def _voltage_bases(self, cmd: List[str]) -> None:
        bases = [self.model.nominal_kv]
        for t in self.model.transformers:
            for kv in (t.kv_high, t.kv_low):
                if kv and kv not in bases:
                    bases.append(kv)
        cmd.append("Set VoltageBases=[" + ", ".join(f"{b:.6g}" for b in bases) + "]")
        cmd.append("CalcVoltageBases")