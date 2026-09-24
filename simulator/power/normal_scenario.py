#!/usr/bin/env python3
"""Normal-scenario generator (Phase D).

Binds Ausgrid customer load profiles to a feeder's CGM loads (canonical uid
order from ``ComponentModel.get_load_targets``), runs a power flow at every
half-hour timestamp via OpenDSS (``opendssdirect``), and writes bus-level
voltage telemetry plus a summary and a machine-readable manifest.

Usage
-----
::

    python3 -m simulator.power.normal_scenario \\
        --feeder ieee123 \\
        --assignment grid_data/profile_assignment_85_seed42.csv \\
        --start 2010-07-01T00:00:00 --days 7 \\
        --out results/ieee123

The power-flow engine import (``opendssdirect``) is deferred to ``run`` so
the module can be imported by tests without OpenDSS installed.

Telemetry contract (manifest ``schema``):
  - every row is an interval-start timestamp, naive local (AEST/AEDT);
  - ``vpu_AB/BC/CA`` are line-to-line per-unit magnitudes on the bus's own
    nominal voltage base (delta buses included);
  - ``i_amps`` is the RMS magnitude (A) of the phase current flowing *into*
    each line/switch at its ``bus1`` (send-end) terminal, and of the
    ``Vsource`` at ``SYSsource`` (substation feeder head);
  - summary rows carry source active/reactive power and the OpenDSS
    convergence flag.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from typing import Dict, List, Optional


_PHASE_PAIRS = {("1", "2"): "AB", ("2", "3"): "BC", ("3", "1"): "CA"}
_PHASE_FROM_NODE = {1: "A", 2: "B", 3: "C"}


def _ensure_grid_data() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    grid_data = os.path.join(here, "grid_data")
    for p in (grid_data, here):
        if p not in sys.path:
            sys.path.insert(0, p)
    return grid_data


class _ProfileStore:
    """Half-hour kw profiles per customer, read once and held in memory."""

    def __init__(self, profiles_csv: str, customer_ids: List[int]):
        _ensure_grid_data()
        import profile_engine  # grid_data on sys.path

        engine = profile_engine.ProfileEngine(profiles_csv)
        self._rows: Dict[int, Dict[str, float]] = {}
        self._mean: Dict[int, float] = {}
        for cid in customer_ids:
            prof = engine.get_customer_profile(cid)
            if prof is None:
                raise RuntimeError(f"profile missing for customer {cid}")
            kw = {s.timestamp.isoformat(sep="T"): s.load_kw for s in prof.samples}
            self._rows[cid] = kw
            self._mean[cid] = sum(kw.values()) / len(kw) if kw else 1.0
        if self._rows:
            common = set.intersection(*[set(v) for v in self._rows.values()])
            self._ts = sorted(common)
        else:
            self._ts = []

    def timestamps(self) -> List[str]:
        return self._ts

    def shape_at(self, customer_id: int, ts: str) -> float:
        mean = self._mean[customer_id]
        if not mean:
            return 1.0
        return self._rows[customer_id].get(ts, mean) / mean


def _read_assignment(path: str) -> List[int]:
    with open(path, newline="") as f:
        return [int(r["customer_id"]) for r in csv.DictReader(f)]


def _read_assignment_seed(path: str) -> Optional[int]:
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            return int(row["seed"])
    return None


def _resolved_transformer_buses(model, t) -> List[str]:
    from simulator.power.dss_builder import load_bus_overrides

    buses = [str(b) for b in t.buses]
    if all(buses):
        return buses
    override = load_bus_overrides().get((model.id, t.source_id))
    return list(override) if override else buses


def _bus_nominal_kv(model) -> Dict[str, float]:
    """Nominal kV per bus: feeder level plus transformer terminal levels."""
    kv: Dict[str, float] = {bus.source_id: float(model.nominal_kv) for bus in model.buses}
    for t in model.transformers:
        buses = _resolved_transformer_buses(model, t)
        if any(".hv" in b for b in buses):
            lo = [b for b in buses if ".hv" not in b][0]
            kv[lo] = float(t.kv_low)
            kv[f"{lo}.hv"] = float(t.kv_high)
            continue
        def _is_high(bus):
            from simulator.power.dss_builder import DssBuilder
            endpoints = {bus for line in model.lines for bus in (line.bus1, line.bus2)}
            return DssBuilder._terminal_order(t, buses, endpoints,
                                              float(model.nominal_kv))[0] == bus
        kv[buses[0]] = float(t.kv_high) if _is_high(buses[0]) else float(t.kv_low)
        kv[buses[1]] = float(t.kv_low) if _is_high(buses[0]) else float(t.kv_high)
    kv.pop("SYSsource", None)
    return kv


def _order_load_uids(model) -> List[str]:
    targets = model.get_load_targets()
    for feeder_id, loads in targets.items():
        if feeder_id == model.id:
            return list(loads.keys())
    return list(targets.values())[0].keys() if targets else []


def _rated_kvar(model) -> Dict[str, List[float]]:
    """uid -> [phase kvar in element emission order (same as load_element_map)]. """
    out: Dict[str, List[float]] = {}
    for load in model.loads:
        out[load.uid] = [load.kvar_per_phase.get(p, 0.0) for p in ("A", "B", "C")]
    return out


def _load_element_kv(model, bus_name: str) -> float:
    return float(model.nominal_kv)


def _floating_stubs(model) -> set:
    """/secondary transformer terminal buses with no connected load, line or
    switch -- they float when the transformer is unloaded and their per-unit
    voltage is not a meaningful network metric."""
    from simulator.power.dss_builder import DssBuilder

    used = {line.bus1 for line in model.lines} | {line.bus2 for line in model.lines}
    used |= {load.bus for load in model.loads}
    used |= {cap.bus for cap in model.capacitors}
    endpoints = {bus for line in model.lines for bus in (line.bus1, line.bus2)}
    stubs: set = set()
    for t in model.transformers:
        if any(".hv" in b for b in t.buses):
            continue
        buses = _resolved_transformer_buses(model, t)
        lo = DssBuilder._terminal_order(
            t, buses, endpoints, float(model.nominal_kv))[1]
        if lo not in used:
            stubs.add(lo)
    return stubs


class _CurrentMeter:
    """Cached descriptors of the telemetry current elements (every line/switch
    plus the source), so per-timestamp reads only re-query ``Currents()``.

    Measurement location: each branch is metered at its ``bus1`` (send-end)
    terminal -- ``i_amps`` is the RMS magnitude of the current flowing *into*
    the element there, per physical phase node.  The source element is metered
    at ``SYSsource`` (substation feeder head).
    """

    def __init__(self, dss) -> None:
        self._elements: List[Tuple[str, str, str, Tuple[str, float]]] = []
        for name in dss.Circuit.AllElementNames():
            lower = name.lower()
            if not (lower.startswith("line.") or lower.startswith("vsource.")):
                continue
            dss.Circuit.SetActiveElement(name)
            node_order = list(dss.CktElement.NodeOrder())
            n_phases = dss.CktElement.NumPhases()
            bus_names = list(dss.CktElement.BusNames())
            if len(node_order) < n_phases or not bus_names:
                continue
            send_nodes = node_order[:n_phases]
            phases = tuple(
                (ph, pos) for pos, node in enumerate(send_nodes)
                if (ph := _PHASE_FROM_NODE.get(int(node))) is not None
            )
            if not phases:
                continue
            if lower.startswith("vsource."):
                kind = "source"
            elif lower.startswith("line.sw_"):
                kind = "switch"
            else:
                kind = "line"
            self._elements.append((name, kind, bus_names[0], phases))

    def read(self, dss, timestamp: str, feeder_id: str,
             writer) -> None:
        for name, kind, bus, phases in self._elements:
            dss.Circuit.SetActiveElement(name)
            currents = dss.CktElement.Currents()
            for ph, pos in phases:
                i_amp = abs(complex(currents[2 * pos],
                                    currents[2 * pos + 1]))
                writer.writerow([timestamp, feeder_id, name, kind, bus,
                                 ph, f"{i_amp:.3f}"])


def run(feeder_id: str, assignment_csv: str, profiles_csv: str,
        start: dt.datetime, days: int, out_dir: str,
        verbose: bool = False) -> Dict[str, object]:
    from simulator.power.loader import FeederLoader
    from simulator.power.dss_builder import DssBuilder
    import opendssdirect as dss

    model = FeederLoader().load(feeder_id)
    customers = _read_assignment(assignment_csv)
    uids = _order_load_uids(model)
    if len(uids) != len(customers):
        raise RuntimeError(
            f"assignment count {len(customers)} != load-target count "
            f"{len(uids)} for feeder {feeder_id}"
        )

    store = _ProfileStore(profiles_csv, customers)
    ts_list = store.timestamps()
    if not ts_list:
        raise RuntimeError("no common timestamps across assigned customers")
    start = start or dt.datetime.fromisoformat(ts_list[0])
    end = start + dt.timedelta(days=days)
    window = [t for t in ts_list if start <= dt.datetime.fromisoformat(t) < end]
    if not window:
        raise RuntimeError("no profile timestamps in requested window")

    builder = DssBuilder(model)
    dss.Text.Commands(builder.build_commands())

    elem_map = builder.load_element_map()
    kvar_all = _rated_kvar(model)
    rated_kw = {u: sum(e[2] for e in elem_map[u]) for u in uids}

    bus_kv = _bus_nominal_kv(model)
    skip = {"SYSsource", "syssource"} | _floating_stubs(model)

    os.makedirs(out_dir, exist_ok=True)
    bus_csv = os.path.join(out_dir, f"normal_{feeder_id}_bus_telemetry.csv")
    sum_csv = os.path.join(out_dir, f"normal_{feeder_id}_summary.csv")
    cur_csv = os.path.join(out_dir, f"normal_{feeder_id}_current_telemetry.csv")
    with open(bus_csv, "w", newline="") as bf, open(sum_csv, "w", newline="") as sf, \
            open(cur_csv, "w", newline="") as cf:
        bw = csv.writer(bf)
        bw.writerow(["timestamp", "feeder_id", "bus", "vpu_AB", "vpu_BC", "vpu_CA"])
        sw = csv.writer(sf)
        sw.writerow(["timestamp", "feeder_id", "source_p_kw", "source_q_kvar",
                     "converged", "n_buses", "n_deenergized", "vpu_min", "vpu_max"])
        cw = csv.writer(cf)
        cw.writerow(["timestamp", "feeder_id", "element", "element_type",
                     "bus", "phase", "i_amps"])
        meter = _CurrentMeter(dss)
        for i, ts in enumerate(window):
            shape = {c: store.shape_at(c, ts) for c in customers}
            kw_updates: Dict[str, Dict[str, float]] = {}
            kvar_updates: Dict[str, Dict[str, float]] = {}
            for j, uid in enumerate(uids):
                cust = customers[j]
                s = shape[cust]
                kw_ph: Dict[str, float] = {}
                kvar_ph: Dict[str, float] = {}
                for (el, phase, ph_kw) in elem_map[uid]:
                    kw_ph[phase] = ph_kw * s
                    kvar_ph[phase] = s * kvar_all[uid][("A", "B", "C").index(phase)]
                kw_updates[uid] = kw_ph
                kvar_updates[uid] = kvar_ph
            dss.Text.Commands(builder.update_load_commands(kw_updates, kvar_updates))
            dss.Solution.Solve()
            converged = dss.Solution.Converged()
            tp = dss.Circuit.TotalPower()
            src_p = -float(tp[0])
            src_q = -float(tp[1])
            meter.read(dss, ts, feeder_id, cw)
            tele = {}
            for name in dss.Circuit.AllBusNames():
                if name in skip:
                    continue
                base = bus_kv.get(name)
                if not base:
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
                        pairs.append(abs(nodes[i1] - nodes[i2]) / (base * 1000.0))
                tele[name] = pairs
            all_v = [v for vals in tele.values() for v in vals]
            de = 0
            energ = []
            for vals in tele.values():
                if vals and max(vals) < 0.5:
                    de += 1
                else:
                    energ.extend(vals)
            for bus in sorted(tele):
                vals = tele[bus] + [""] * (3 - len(tele[bus]))
                bw.writerow([ts, feeder_id, bus] + vals)
            sw.writerow([
                ts, feeder_id, f"{src_p:.2f}", f"{src_q:.2f}", int(converged),
                len(tele), de,
                f"{min(energ):.4f}" if energ else "",
                f"{max(energ):.4f}" if energ else "",
            ])
            if verbose and (i % 48 == 0 or i == len(window) - 1):
                print(f"  t={ts} P={src_p:.1f}kW Q={src_q:.1f}kvar "
                      f"conv={converged} n_bus={len(tele)} de={de} "
                      f"vpu=[{min(energ):.3f},{max(energ):.3f}]")

    manifest = {
        "scenario": "normal",
        "feeder_id": feeder_id,
        "excluded_buses": {
            "source": "SYSsource (virtual source bus), floating unloaded "
                      "transformer secondary stub buses",
            "names": sorted(skip),
        },
        "assignment_csv": assignment_csv,
        "profiles_csv": os.path.basename(profiles_csv),
        "window": {"start": start.isoformat(sep="T"), "days": days,
                   "steps": len(window)},
        "binding": {
            "order": "canonical get_load_targets uid order, aligned to "
                     "assignment-file row order",
            "count": len(uids), "customers": customers,
            "seed": _read_assignment_seed(assignment_csv),
        },
        "load_model": {
            "kind": "shape-scaling around rated phase power",
            "element_kw_formula": "rated_kw * (profile_kw(ts) / profile_mean)",
            "element_kvar_formula": "rated_kvar * (profile_kw(ts) / profile_mean)",
        },
        "power_flow": {
            "engine": "OpenDSS via opendssdirect.py",
            "flow": "per-timestamp full AC solve, ControlMode=OFF",
            "regulators": "held at nominal tap (CGM has no r-bus splits)",
        },
        "telemetry": {
            "bus_csv": bus_csv,
            "summary_csv": sum_csv,
            "current_csv": cur_csv,
            "schema": {
                "timestamp": "interval-start, naive local AEST/AEDT",
                "vpu_AB/BC/CA": "line-to-line per-unit magnitude on bus base",
                "source_p_kw": "active power into the feeder at Vsource",
                "source_q_kvar": "reactive power into the feeder at Vsource",
                "i_amps": "RMS current magnitude (A) at the send-end terminal",
            },
            "current_location": "phase current flowing into each line/switch "
                               "element at its bus1 terminal; Vsource.Source "
                               "phase currents at SYSsource (substation "
                               "feeder head)",
        },
        "builder_issues": list(dict.fromkeys(builder.issues)),
    }
    with open(os.path.join(out_dir, f"normal_{feeder_id}_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feeder", required=True, help="ieee37 | ieee123")
    ap.add_argument("--assignment", required=True, help="B-8 assignment CSV path")
    ap.add_argument("--profiles", default=os.path.join(
        os.path.dirname(__file__), "..", "..", "grid_data",
        "ausgrid_profiles_2010_2011.csv"))
    ap.add_argument("--start", default="2010-07-01T00:00:00")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    grid_data = _ensure_grid_data()
    start = dt.datetime.fromisoformat(args.start)
    manifest = run(args.feeder, args.assignment, args.profiles,
                   start, args.days, args.out, verbose=args.verbose)
    print(json.dumps({"ok": True, "out": args.out,
                      "steps": manifest["window"]["steps"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())