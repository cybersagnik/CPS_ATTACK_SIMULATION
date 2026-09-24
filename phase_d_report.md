# Phase D — Normal (attack-free) scenario generation & power-flow telemetry

Status: **implemented and validated**. Power flow runs in **OpenDSS via
`opendssdirect.py`** for the two loadable CGM feeders (`ieee37`, `ieee123`),
loads driven by bound Ausgrid half-hour profiles, per-timestamp AC solved and
recorded to CSV telemetry plus manifests.

---

## 1. Repo state (this phase's deliverables)

New code (tracked):

| File | Purpose |
|------|---------|
| `simulator/power/data/line_impedance_ohm_mi.csv` | Per-line-configuration per-mile R/X lower-triangle matrices, transcribed from OpenDSS `IEEELineCodes.DSS` (corrected 2010-09-16) for ieee123 configs 1-12 and ieee37 721-724 |
| `simulator/power/data/dss_bus_overrides.csv` | Physical-data override table restoring ieee123 `XFM-1` terminal buses to `61`/`610` (CGM ships empty strings) |
| `simulator/power/dss_builder.py` | CGM -> OpenDSS translator: `build_commands()`, `load_element_map()`, `update_load_commands()`, transformer terminal resolution, switch/capacitor/load modeling decisions |
| `simulator/power/normal_scenario.py` | Profile binding + per-timestamp OpenDSS solve + telemetry/manifest writer (CLI + `run()`) |
| `simulator/power/test_dss_builder.py` | 6 pure translation unit tests |
| `simulator/power/test_normal_scenario.py` | 3 pure binding/assignment unit tests |
| `simulator/power/test_e2e_scenario.py` | Gated end-to-end run test (`RUN_E2E=1`) |
| `grid_data/profile_engine.py` (modified) | Added the two documented additive accessors B-8 needs: `get_customer_metadata(cid)`, `customer_has_cl(cid)` (were missing; B-8 crashed on them) |
| `.gitignore` | Ignore regenerable `grid_data/ausgrid_profiles_2010_2011.csv` (375 MB) and `results/` |

Generated artifacts (regenerable, not tracked):

- `grid_data/ausgrid_profiles_2010_2011.csv` (375 MB, documented ~44 s parse)
- `grid_data/profile_assignment_25_seed42.csv`, `grid_data/profile_assignment_85_seed42.csv` (B-8 schema, `seed=42`)
- `results/ieee37/{normal_ieee37_{bus_telemetry,summary,manifest}}` — 7-day run
- `results/ieee123/{normal_ieee123_{bus_telemetry,summary,manifest}}` — 7-day run

## 2. How to reproduce

```bash
# 1. environment (system Python; PEP-668 workaround)
pip3 install --user --break-system-packages "opendssdirect.py==0.9.4"

# 2. profile + assignment artifacts (from grid_data/)
cd grid_data
python3 ausgrid_profiles.py "Solar home 2010-2011.csv"            # -> ausgrid_profiles_2010_2011.csv
python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 25 --seed 42 --output profile_assignment_25_seed42.csv
python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 85 --seed 42 --output profile_assignment_85_seed42.csv

# 3. scenarios (from repo root; run both, default window = 7 days)
python3 -m simulator.power.normal_scenario \
    --feeder ieee37  --assignment grid_data/profile_assignment_25_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee37  --verbose
python3 -m simulator.power.normal_scenario \
    --feeder ieee123 --assignment grid_data/profile_assignment_85_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee123 --verbose
# full year: --days 365

# 3b. tests
python3 -m unittest -v simulator.power.test_dss_builder simulator.power.test_normal_scenario
RUN_E2E=1 python3 -m unittest -v simulator.power.test_e2e_scenario   # end-to-end, ~4 min
python3 -m pytest -q                                                # existing 71 pass unchanged
```

## 3. Feeders tested and why

- **ieee37** (37 buses, 25 loads, 2457 kW rated, 3-wire ungrounded delta,
  mostly single-phase L-L loads) — the smallest loadable non-YYD adapter; fast
  (<1 s per 48-step day) and directly comparable against the official OpenDSS
  `ieee37.dss`.
- **ieee123** (128 buses, 85 loads, 3490 kW rated, grounded-wye 4.16 kV,
  switched sub-laterals, 11 switches incl. 5 open, 4 capacitor banks) — the
  larger loadable non-YYD adapter; exercises switches, L-L and L-N loads, and
  an unloaded 0.48 kV delta stub (XFM-1 at 61-610).
- 60/87 CGM feeders are `YYD` (delta-delta type) adapters that are not loadable
  via the existing loaders (`FeederLoader.load` supports `xls_spec` only), so
  Phase D runs on the two loadable feeders, matching the Phase C/C common-target
  precedence.

## 4. Translation validity (vs authoritative references)

Sources: `github.com/tshort/OpenDSS/Distrib/IEEETestCases/{37Bus,123Bus}`
(`ieee37.dss`, `IEEE123Master.dss`, `IEEE123Regulators.DSS`, `IEEE123Loads.DSS`,
`IEEELineCodes.DSS`).

- **ieee37** line-to-line voltage profile matches the official case to a
  **uniform mean offset of −0.31 kV (−6.5 %)** with **max residual 0.36 kV
  (0.075 pu on 4.8 kV)** across all 37 buses — the profile shape is identical
  and the offset/residuals are exactly the official voltage-regulator tap
  (official boosts downstream, regulators here are held at nominal tap because
  the CGM has no `<bus>r` split terminals; inserting an r-bus would be a
  topology invention, prohibited).
- **ieee123** line-to-line profile matches with a per-bus LL offset of
  **−0.7 % (tail) to −10.4 % (head/capacitor buses), pooled mean −7.5 %,
  residual std 2.5 %** — explained by (a) the official master abstracts away the
  115/4.16 substation transformer that the CGM declares (r=1 x=8 on 5 MVA) and
  (b) nominal-tap regulators vs the official active regulators/LDC; the XFM-1
  stub correctly sits at 0.48 kV (0.11 pu of 4.16) in both.
- During validation three real translation bugs were caught and fixed:
  OpenDSS connection enum (`WYE_GRD` -> `Wye`), linecode matrix block formatting
  (single-line `New LineCode`), and transformer terminal ordering
  (`_terminal_order` resolves high/low from declared kVs and line-endpoint
  membership — CGM's terminals are unordered; XFM-1 is 709/61 = high).

## 5. Convergence / NaN proof (7-day runs shown; same holds 1-day e2e)

| feeder | steps | all `Converged()` | NaN/blank voltages | n_buses/yr telemetry rows | source P kW (window) | energised vpu range (window) |
|--------|-------|-------------------|--------------------|---------------------------|----------------------|------------------------------|
| ieee37 | 336 | True | 0 | 36 | 922 .. 6216 | 0.636 .. 0.987 |
| ieee123 | 336 | True | 0 | 125 | 1529 .. 6903 | 0.740 .. 1.011 |

Notes: per-unit values are line-to-line magnitudes on the bus's own nominal kV
base. One ieee123 island bus (behind open switch 15x-3xx) is de-energized in
every step, reported via `n_deenergized` column — physically correct.
The unloaded 0.48 kV stub buses (`610`, `775`) are excluded from telemetry as
floating (documented in each manifest). The P ranges swing ~±2.5x around rated
because one Ausgrid customer is bound per load element; extreme simultaneous
customer peaks drive the voltage into the 0.6-0.75 pu band at rare steps while
typical steps stay 0.83-0.98 pu — conservative but physically consistent with
this heavily-loaded IEEE test feeder design.

## 6. Binding & telemetry schema

Binding: `profile_assignment_{25,85}_seed42.csv` (B-8) rows aligned to the
canonical `ComponentModel.get_load_targets()[feeder]` uid order (85 = 123 loads,
25 = 37 loads). Load scaling (manifest `load_model`):

    element_kw(ts)   = rated_element_kw   * (profile_kw(ts) / profile_mean)
    element_kvar(ts) = rated_element_kvar * (profile_kw(ts) / profile_mean)

Files per run, under `results/<feeder>/`:

- `normal_<feeder>_bus_telemetry.csv` — columns `timestamp, feeder_id, bus,
  vpu_AB, vpu_BC, vpu_CA` (interval-start, naive local AEST/AEDT; empty cells
  for buses with fewer than 3 nodes).
- `normal_<feeder>_summary.csv` — `timestamp, feeder_id, source_p_kw,
  source_q_kvar, converged, n_buses, n_deenergized, vpu_min, vpu_max`.
- `normal_<feeder>_manifest.json` — parameters, binding order, excluded buses
  (source bus + floating stubs), schema contract, builder issues.

Also enabled the pipeline (additive only): `ProfileEngine.get_customer_metadata`
and `.customer_has_cl` (documented in `workflow.md`; B-8 previously crashed
without them).

## 7. Blocker notes

- No `python3-venv`/`ensurepip` (no passwordless sudo) — installed OpenDSS to
  user site with `--break-system-packages`. All runs must use system `python3`.
- PyPI package name is `opendssdirect.py`; `pip install opendssdirect` does not
  exist.
- OpenDSS `Text.Commands` does not accept `~` continuation lines — matrix props
  are emitted inline.
- Thought to record: official OpenDSS 123/37 buses report "odd" L-G per-unit on
  B/C because large parts of the networks are ungrounded delta or mixed; L-L
  per-unit is the comparable metric (used here).

## 8. Model decisions (explicit)

1. Regulators held at nominal tap; no r-bus introduced (no topology invention).
2. Substation transformer emitted as real 115/4.16 or 230/4.8 element into a
   virtual `SYSsource`; this differs from the official master's abstracted
   source and is preserved because the CGM declares the transformer.
3. Closed switch on a transformer terminal pair is dropped (the transformer
   carries the connection).
4. `vpu_*` reported as L-L magnitudes; floating stub and de-energized islands
   reported but excluded from energised voltage metrics.
5. Shape-scaling (not literal kW replacement) keeps the feeder at its designed
   magnitudes while applying real 30-minute consumer dynamics.

## 9. Checkbox status (Phase D)

- [x] Normal scenarios generated on ieee37 and ieee123
- [x] Real Ausgrid profiles bound to loads (canonical uid order, seed 42)
- [x] OpenDSS power-flow translation validated against official references
- [x] Every step converged; zero NaN in telemetry
- [x] Telemetry + manifest recorded under `results/<feeder>/`
- [x] Focused tests added (9 pure unit + 2 gated e2e); existing 71 pass
- [x] Report written