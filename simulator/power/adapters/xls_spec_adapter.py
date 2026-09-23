"""Kersting spreadsheet adapter for the classic IEEE test feeders.

Handles the ``spec_data`` XLS family (IEEE 13/34/37 share the same workbook
layout: one workbook per component type; IEEE-37 implemented here).

Only the IEEE-37 manifest is registered for now; the parser machinery is
general enough that IEEE-13/34/123 manifests can be added with a sheet-name
map (their sheet names differ, e.g. ``cap data.xls``/``switch data.xls``).

No attack behaviour lives here -- the adapter only extracts the topology into
:class:`~simulator.power.common_model.FeederModel`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xlrd

from ..common_model import (
    Bus,
    Capacitor,
    FeederModel,
    Line,
    LineConfiguration,
    Load,
    Measurement,
    Regulator,
    Switch,
    Transformer,
)
from ..feeder_registry import FeederSource
from .base import FeederAdapter

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")


# --------------------------------------------------------------------------- #
# small cell helpers
# --------------------------------------------------------------------------- #


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUM_RE.search(value.replace(",", ""))
        return float(match.group()) if match else None
    return None


def _node_id(value: Any) -> str:
    value = _cell(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _phases(value: Any) -> List[str]:
    return [c for c in str(_cell(value)).upper() if c in "ABC"]


def _phase_presence(value: Any) -> List[str]:
    """Live phases present on a configuration (sorted, neutral excluded).

    ``"C A B N"`` -> ``["A", "B", "C"]``; ``"A N"`` -> ``["A"]``;
    ``"A C N"`` -> ``["A", "C"]``.  The neutral/order info (used later for
    impedance derivation) stays in the raw ``phasing`` source cell.
    """
    return sorted(set(c for c in str(_cell(value)).upper() if c in "ABC"))


def _conn_of(kv: str) -> str:
    upper = str(kv).upper().replace(" ", "")
    if "GR" in upper and ("W" in upper or "Y" in upper):
        return "WYE_GRD"
    if ("D" in upper) and ("W" not in upper) and ("Y" not in upper):
        return "DELTA"
    if "W" in upper or "Y" in upper:
        return "WYE"
    return "UNKNOWN"


def _kv_split(kv: str) -> Tuple[float, str]:
    return (_to_float(kv) or 0.0, _conn_of(kv))


def _find_col(values: List[Any], tokens: Tuple[str, ...]) -> Optional[int]:
    for i, v in enumerate(values):
        s = str(_cell(v))
        if any(s.startswith(tok) or s == tok for tok in tokens):
            return i
    return None


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #


class KerstingXlsAdapter(FeederAdapter):
    """Common model extraction from the Kersting ``spec_data`` XLS workbooks."""

    status_kinds: Tuple[str, ...] = ("spec_data",)

    #: per-feeder manifest: optional sheets per component + facts.
    #: ``configs`` may be a bare filename (one sheet) or a list of
    #: ``(kind, filename)`` pairs (overhead + underground sheets).
    SUPPORTED: Dict[str, Dict[str, Any]] = {
        "ieee37": {
            "nominal_kv": 4.8,
            "frequency_hz": 60.0,
            "sheets": {
                "lines": "Line Data.xls",
                "loads": "Spot Loads.xls",
                "regulators": "Regulator Data.xls",
                "transformers": "Transformer Data.xls",
                "configs": "UG Config.xls",
            },
        },
        "ieee123": {
            "nominal_kv": 4.16,
            "frequency_hz": 60.0,
            "sheets": {
                "lines": "line data.xls",
                "loads": "spot loads data.xls",
                "regulators": "Regulator Data.xls",
                "transformers": "Transformer Data.xls",
                "configs": [
                    ("overhead", "config data.xls"),
                    ("underground", "UG configuration data.xls"),
                ],
                "capacitors": "cap data.xls",
                "switches": "switch data.xls",
            },
        },
    }

    def supported_ids(self) -> List[str]:
        """Feeder ids this adapter has actual manifests for (loadable)."""
        return sorted(self.SUPPORTED)

    def load(self, source: FeederSource) -> FeederModel:
        manifest = self.SUPPORTED.get(source.id)
        if manifest is None:
            raise NotImplementedError(
                f"KerstingXlsAdapter has no manifest for feeder {source.id!r}; "
                f"supported: {sorted(self.SUPPORTED)}"
            )

        sheets = manifest["sheets"]
        model = FeederModel(
            id=source.id,
            name=source.name,
            nominal_kv=float(manifest["nominal_kv"]),
            frequency_hz=float(manifest["frequency_hz"]),
        )

        data_dir = Path(source.dir)
        if not data_dir.is_dir():
            raise FileNotFoundError(f"Feeder directory missing: {data_dir}")

        self._uid = {c: 0 for c in (
            "bus", "line", "line_configuration", "transformer",
            "regulator", "capacitor", "switch", "load", "measurement",
        )}

        model.configurations = self._parse_all_configs(data_dir, sheets.get("configs"), model.warnings)

        xfmr_links: List[Tuple[str, str, str]] = []

        lines_sheet = self._open_sheet(data_dir, sheets.get("lines"))
        if lines_sheet is not None:
            model.lines, xfmr_links = self._parse_lines(lines_sheet, model.configurations)
        else:
            model.warnings.append("lines sheet missing")

        loads_sheet = self._open_sheet(data_dir, sheets.get("loads"))
        if loads_sheet is not None:
            model.loads = self._parse_loads(loads_sheet)
        else:
            model.warnings.append("loads sheet missing")

        reg_sheet = self._open_sheet(data_dir, sheets.get("regulators"))
        if reg_sheet is not None:
            model.regulators = self._parse_regulators(reg_sheet)
        else:
            model.warnings.append("regulators sheet missing")

        tf_sheet = self._open_sheet(data_dir, sheets.get("transformers"))
        model.transformers = (
            self._parse_transformers(tf_sheet, xfmr_links, model)
            if tf_sheet is not None
            else []
        )

        cap_sheet = self._open_sheet(data_dir, sheets.get("capacitors"))
        if cap_sheet is not None:
            model.capacitors = self._parse_capacitors(cap_sheet)
        else:
            model.warnings.append("capacitors sheet missing (no capacitors)")

        switch_sheet = self._open_sheet(data_dir, sheets.get("switches"))
        if switch_sheet is not None:
            model.switches = self._parse_switches(switch_sheet)
        else:
            model.warnings.append("switches sheet missing (no switches)")

        model.buses = self._build_buses(model)
        model.build_index()
        model.measurements = self._synthesize_measurements(model)
        return model

    # -- sheet access ------------------------------------------------------ #
    def _open_sheet(self, data_dir: Path, filename: Optional[str]) -> Any:
        if not filename:
            return None
        path = data_dir / filename
        if not path.is_file():
            matches = list(data_dir.rglob(filename))
            path = matches[0] if len(matches) == 1 else None
            if path is None:
                return None
        workbook = xlrd.open_workbook(path)
        if "Sheet1" in workbook.sheet_names():
            return workbook.sheet_by_name("Sheet1")
        candidates = [s for s in workbook.sheets() if s.nrows > 0]
        return candidates[0] if candidates else None

    def _uid_for(self, concept: str) -> str:
        self._uid[concept] += 1
        return f"{concept}/{self._uid[concept]:03d}"

    # -- lines ------------------------------------------------------------- #
    def _parse_lines(
        self, sheet, configurations: List[LineConfiguration]
    ) -> Tuple[List[Line], List[Tuple[str, str, str]]]:
        phases_by_config = {
            c.source_id: list(c.phases)
            for c in configurations
            if c.phases
        }
        lines: List[Line] = []
        links: List[Tuple[str, str, str]] = []
        start = self._table_start(
            sheet, ("Node A",), ("Length",), ("Config",)
        )
        if start is None:
            return lines, links
        col = {
            "a": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Node A",)),
            "b": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Node B",)),
            "len": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Length",)),
            "cfg": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Config",)),
        }
        for r in range(start + 1, sheet.nrows):
            a = _node_id(sheet.cell_value(r, col["a"]))
            b = _node_id(sheet.cell_value(r, col["b"]))
            if not a or not b:
                continue
            cfg_raw = _cell(sheet.cell_value(r, col["cfg"]))
            length = _to_float(sheet.cell_value(r, col["len"])) or 0.0
            if isinstance(cfg_raw, (int, float)):
                cfg_id = str(int(cfg_raw))
                lines.append(
                    Line(
                        uid=self._uid_for("line"),
                        source_id=f"{a}-{b}",
                        bus1=a,
                        bus2=b,
                        phases=phases_by_config.get(cfg_id, ["A", "B", "C"]),
                        length_ft=length,
                        config_source_id=cfg_id,
                    )
                )
            elif cfg_raw:
                # non-numeric config -> transformer / regulator insertion point
                links.append(
                    (a, b, re.sub(r"[^A-Za-z0-9]", "", str(cfg_raw)).upper())
                )
        return lines, links

    # -- configurations ---------------------------------------------------- #
    def _parse_all_configs(
        self, data_dir: Path, declared: Any, warnings: List[str]
    ) -> List[LineConfiguration]:
        configs: List[LineConfiguration] = []
        if declared is None:
            warnings.append("configs not declared for this feeder")
            return configs

        if isinstance(declared, str):
            sheet = self._open_sheet(data_dir, declared)
            if sheet is None:
                warnings.append(f"configs sheet missing: {declared}")
            else:
                configs.extend(self._parse_configs(sheet, kind="underground"))
            return configs

        for kind, filename in declared:
            sheet = self._open_sheet(data_dir, filename)
            if sheet is None:
                warnings.append(f"configs sheet missing: {filename}")
                continue
            configs.extend(self._parse_configs(sheet, kind=kind))
        return configs

    def _parse_configs(self, sheet, kind: str) -> List[LineConfiguration]:
        configs: List[LineConfiguration] = []
        start = self._table_start(sheet, ("Config",), ("Phasing",), ("Spacing",))
        if start is None:
            return configs
        col = {
            "id": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Config",)),
            "ph": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Phasing",)),
            "cond": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Cable", "Phase", "Phase Cond")),
            "sp": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Spacing",)),
        }
        for r in range(start + 1, sheet.nrows):
            raw_id = _cell(sheet.cell_value(r, col["id"]))
            if raw_id in ("", None) or isinstance(raw_id, str):
                continue
            raw_ph = str(_cell(sheet.cell_value(r, col["ph"])))
            phases = _phase_presence(raw_ph)
            configs.append(
                LineConfiguration(
                    uid=self._uid_for("line_configuration"),
                    source_id=str(int(raw_id)),
                    kind=kind,
                    phases="".join(phases) if phases else raw_ph,
                    conductor=str(_cell(sheet.cell_value(r, col["cond"]))) if col["cond"] is not None else "",
                    spacing_id=str(_cell(sheet.cell_value(r, col["sp"]))) if col["sp"] is not None else "",
                )
            )
        return configs

    # -- loads ------------------------------------------------------------- #
    def _parse_loads(self, sheet) -> List[Load]:
        loads: List[Load] = []
        header = None
        for r in range(min(sheet.nrows, 8)):
            vals = [_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            if "Node" in vals and "Load" in vals:
                header = r
                break
        if header is None:
            return loads
        cols_ph: List[int] = []
        for r in range(header + 1, min(sheet.nrows, header + 4)):
            vals = [_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            if not any(str(v) in ("kW", "kVAr") for v in vals):
                continue
            if "kW" in vals and cols_ph is None:
                pass
            for c in range(sheet.ncols):
                if str(vals[c]) in ("kW", "kVAr") and c not in cols_ph:
                    cols_ph.append(c)
        # cols_ph now holds 6 indexes: ph1kW, ph1kVAr, ph2kW, ph2kVAr, ph3kW, ph3kVAr
        if len(cols_ph) < 6:
            return loads
        node_col = _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Node",))
        model_col = _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Load",))
        phase_letters = ["A", "B", "C"]
        for r in range(header + 2, sheet.nrows):
            node_raw = _node_id(sheet.cell_value(r, node_col))
            model_raw = str(_cell(sheet.cell_value(r, model_col)))
            if not node_raw or node_raw.lower() == "total" or model_raw.lower().startswith("total"):
                continue
            kw, kvar = {}, {}
            for i in range(3):
                kw[phase_letters[i]] = _to_float(sheet.cell_value(r, cols_ph[2 * i])) or 0.0
                kvar[phase_letters[i]] = _to_float(sheet.cell_value(r, cols_ph[2 * i + 1])) or 0.0
            active = [p for p in phase_letters if kw[p] or kvar[p]]
            if not active:
                continue
            connection = "delta" if model_raw.upper().startswith("D") else "wye"
            loads.append(
                Load(
                    uid=self._uid_for("load"),
                    source_id=node_raw,
                    bus=node_raw,
                    phases=active,
                    load_model=model_raw.upper().split("-")[-1].strip() if "-" in model_raw else model_raw.upper(),
                    connection=connection,
                    kw_per_phase=kw,
                    kvar_per_phase=kvar,
                )
            )
        return loads

    # -- regulators -------------------------------------------------------- #
    def _parse_regulators(self, sheet) -> List[Regulator]:
        regulators: List[Regulator] = []
        blocks: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        key_map = {
            "regulatorid": "id",
            "linesegment": "line_segment",
            "location": "location",
            "phases": "phases",
            "connection": "connection",
            "bandwidth": "bandwidth",
            "ptratio": "pt_ratio",
            "primaryctrating": "ct_rating",
            "rsetting": "comp_r",
            "xsetting": "comp_x",
            "voltagelevel": "voltage_level",
        }
        for r in range(sheet.nrows):
            vals = [_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            label = str(vals[0]) if vals else ""
            canonical = re.sub(r"[^a-z]", "", label.lower())
            if canonical in ("", "regulatordata"):
                continue
            if "regulatorid" in canonical:
                current = {}
                blocks.append(current)
            if current is None:
                continue
            field = key_map.get(canonical)
            if field is None:
                continue
            value = next((v for v in vals[1:] if str(_cell(v)).strip() != ""), None)
            if value is None:
                continue
            current[field] = value

        for block in blocks:
            phases = _phases(block.get("phases"))
            location = _node_id(block.get("location"))
            regulators.append(
                Regulator(
                    uid=self._uid_for("regulator"),
                    source_id=_node_id(block.get("id")),
                    line_segment=str(_cell(block.get("line_segment", ""))).replace(" ", ""),
                    location=location,
                    phases=phases,
                    connection=str(_cell(block.get("connection", ""))),
                    bandwidth_volts=_to_float(block.get("bandwidth")) or 0.0,
                    pt_ratio=_to_float(block.get("pt_ratio")) or 1.0,
                    ct_rating=_to_float(block.get("ct_rating")) or 0.0,
                    comp_r=_to_float(block.get("comp_r")) or 0.0,
                    comp_x=_to_float(block.get("comp_x")) or 0.0,
                    voltage_level=_to_float(block.get("voltage_level")) or 0.0,
                )
            )
        return regulators

    # -- capacitors / switches --------------------------------------------- #
    def _parse_capacitors(self, sheet) -> List[Capacitor]:
        capacitors: List[Capacitor] = []
        header = None
        for r in range(min(sheet.nrows, 8)):
            vals = [_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            if "Node" in vals and any(str(v).startswith("Ph-A") for v in vals):
                header = r
                break
        if header is None:
            return capacitors
        col = {
            "node": _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Node",)),
            "a": _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Ph-A",)),
            "b": _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Ph-B",)),
            "c": _find_col([_cell(sheet.cell_value(header, c)) for c in range(sheet.ncols)], ("Ph-C",)),
        }
        if any(v is None for v in col.values()):
            return capacitors
        for r in range(header + 2, sheet.nrows):
            node = _node_id(sheet.cell_value(r, col["node"]))
            if not node or node.lower() in ("total", "node"):
                continue
            per_phase = {
                letter: _to_float(sheet.cell_value(r, col[idx])) or 0.0
                for letter, idx in (("A", "a"), ("B", "b"), ("C", "c"))
            }
            active = [p for p, kv in per_phase.items() if kv > 0]
            if not active:
                continue
            capacitors.append(
                Capacitor(
                    uid=self._uid_for("capacitor"),
                    source_id=node,
                    bus=node,
                    phases=active,
                    kvar_per_phase=per_phase,
                )
            )
        return capacitors

    def _parse_switches(self, sheet) -> List[Switch]:
        switches: List[Switch] = []
        start = self._table_start(sheet, ("Node A",), ("Node B",), ("Normal",))
        if start is None:
            return switches
        col = {
            "a": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Node A",)),
            "b": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Node B",)),
            "s": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("Normal",)),
        }
        if any(v is None for v in col.values()):
            return switches
        for r in range(start + 1, sheet.nrows):
            a = _node_id(sheet.cell_value(r, col["a"]))
            b = _node_id(sheet.cell_value(r, col["b"]))
            state = str(_cell(sheet.cell_value(r, col["s"]))).strip().lower()
            if not a or not b or state not in ("open", "closed"):
                continue
            switches.append(
                Switch(
                    uid=self._uid_for("switch"),
                    source_id=f"{a}-{b}",
                    bus1=a,
                    bus2=b,
                    normal_state=state,
                    closed=state == "closed",
                )
            )
        return switches

    # -- transformers ------------------------------------------------------ #
    def _parse_transformers(self, sheet, links, model) -> List[Transformer]:
        transformers: List[Transformer] = []
        link_map = {name: (a, b) for a, b, name in links}
        start = self._table_start(sheet, ("kVA",), ("kV-high",), ("X - %",))
        if start is None:
            return transformers
        col = {
            "name": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("kVA",)),
            "kva": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("kVA",)),
            "kvh": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("kV-high",)),
            "kvl": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("kV-low",)),
            "r": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("R -", "R")),
            "x": _find_col([_cell(sheet.cell_value(start, c)) for c in range(sheet.ncols)], ("X -", "X")),
        }
        p_rows = [r for r in range(start + 1, sheet.nrows)
                  if str(_cell(sheet.cell_value(r, 0))).strip() not in ("",)]
        if not p_rows:
            return transformers
        substation_low = self._substation_low_bus(model, links)
        for r in p_rows:
            raw_name = str(_cell(sheet.cell_value(r, 0)))
            name_key = re.sub(r"[^A-Za-z0-9]", "", raw_name).upper()
            display_name = raw_name.strip().replace(" ", "").replace(":", "")
            kva = _to_float(sheet.cell_value(r, col["kva"])) or 0.0
            kv_high, conn_h = _kv_split(str(_cell(sheet.cell_value(r, col["kvh"]))))
            kv_low, conn_l = _kv_split(str(_cell(sheet.cell_value(r, col["kvl"]))))
            if name_key.startswith("SUBSTATION"):
                buses = (f"{substation_low}.hv", substation_low)
            else:
                buses = link_map.get(name_key, ("", ""))
                if not buses[0]:
                    model.warnings.append(
                        f"transformer {display_name!r}: terminal buses not "
                        "specified in the source sheets (no link row found)"
                    )
            transformers.append(
                Transformer(
                    uid=self._uid_for("transformer"),
                    source_id=display_name,
                    buses=buses,
                    kva=kva,
                    kv_high=kv_high,
                    kv_low=kv_low,
                    connection_high=conn_h,
                    connection_low=conn_l,
                    r_pct=_to_float(sheet.cell_value(r, col["r"])) or 0.0,
                    x_pct=_to_float(sheet.cell_value(r, col["x"])) or 0.0,
                )
            )
        return transformers

    def _substation_low_bus(self, model, links: List[Tuple[str, str, str]]) -> str:
        if model.regulators:
            return model.regulators[0].location
        # fallback: bus with the most incident line connections
        endpoints = [bus for line in model.lines for bus in (line.bus1, line.bus2)]
        endpoints += [bus for a, b, _ in links for bus in (a, b)]
        if not endpoints:
            raise ValueError("cannot infer substation bus (no topology)")
        from collections import Counter

        degree = Counter(endpoints)
        return degree.most_common(1)[0][0]

    # -- buses ------------------------------------------------------------- #
    def _build_buses(self, model) -> List[Bus]:
        seen: Dict[str, Bus] = {}
        endpoints = [bus for line in model.lines for bus in (line.bus1, line.bus2)]
        endpoints += [bus for t in model.transformers for bus in t.buses if bus and ".hv" not in bus]
        endpoints += [load.bus for load in model.loads]
        endpoints += [bus for s in model.switches for bus in (s.bus1, s.bus2)]
        endpoints += [c.bus for c in model.capacitors]
        endpoints += [reg.location for reg in model.regulators if reg.location]
        for node in endpoints:
            if node not in seen:
                seen[node] = Bus(
                    uid=self._uid_for("bus"),
                    source_id=node,
                    phases=["A", "B", "C"],
                    nominal_kv=model.nominal_kv,
                )
        return [seen[n] for n in sorted(seen, key=_node_sort)]

    # -- measurements ------------------------------------------------------ #
    def _synthesize_measurements(self, model) -> List[Measurement]:
        measurements: List[Measurement] = []
        for bus in model.buses:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{bus.source_id}:voltage",
                    kind="voltage",
                    bus=bus.source_id,
                    phases=list(bus.phases),
                    unit="V",
                )
            )
        for line in model.lines:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{line.source_id}:current",
                    kind="current",
                    bus=line.bus1,
                    phases=list(line.phases),
                    unit="A",
                )
            )
        for load in model.loads:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{load.source_id}:power",
                    kind="power",
                    bus=load.bus,
                    phases=list(load.phases),
                    unit="kW",
                )
            )
        for reg in model.regulators:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{reg.source_id}:voltage",
                    kind="voltage",
                    bus=reg.location,
                    phases=[],
                    unit="V",
                )
            )
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{reg.source_id}:tap",
                    kind="tap",
                    bus=reg.location,
                    phases=[],
                    unit="step",
                )
            )
        for cap in model.capacitors:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{cap.source_id}:power",
                    kind="power",
                    bus=cap.bus,
                    phases=list(cap.phases),
                    unit="kVAr",
                )
            )
        for switch in model.switches:
            measurements.append(
                Measurement(
                    uid=self._uid_for("measurement"),
                    source_id=f"{switch.source_id}:state",
                    kind="switch",
                    bus=switch.bus1,
                    phases=[],
                    unit="state",
                )
            )
        return measurements
    @staticmethod
    def _table_start(sheet, *header_tokens: Tuple[str, ...]) -> Optional[int]:
        for r in range(min(sheet.nrows, 12)):
            vals = [_cell(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            if all(_find_col(vals, tokens) is not None for tokens in header_tokens):
                return r
        return None


def _node_sort(node: str) -> Tuple[int, str]:
    """Sort node ids numerically where possible (701 < 7022 < 799)."""
    match = _NUM_RE.fullmatch(node)
    if match:
        return (0, int(match.group()))
    return (1, node)
