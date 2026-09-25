# Workflow — From Energy Diaries to the Attack-Simulation Dataset

*This document explains the end-to-end pipeline in plain language: what each
stage does, why it exists, and the exact command to run it. The technical
reports (`phase_d_report.md`, `phase_e_f_g_h_i_report.md`) contain the full
details and validation evidence; this file is the readable map.*

---

## 0. Pipeline overview

The starting point is the Ausgrid "solar home" trial: 300 households recorded
their electricity use every half hour for one year. The pipeline turns that
diary into a labelled cyber-physical attack dataset:

1. Parse and validate the raw diary, give every reading a timestamp, and merge
   each house into one timeline of load + solar power (steps 1–4).
2. Assign a fixed, seeded set of houses to the customer slots of a test power
   grid, solve the power flow for every half-hour slot, and write the results
   as telemetry (steps 5–7).
3. Turn the telemetry into a SCADA message stream, the way a real operator
   would see it (step 8).
4. Run six MITRE ATT&CK for ICS attack scenarios against that stream, each
   with a measurable physical effect where one applies (step 9).

Everything downstream of step 7 is feeder-agnostic: the same code runs the
ieee37 and ieee123 grids.

```
Ausgrid "Solar home" CSV (raw diary)
    └─ ausgrid_parser.py       clean, validated rows
    └─ ausgrid_timeseries.py   one row per (house, slot)
    └─ ausgrid_profiles.py     one merged load+solar timeline per house (kW)
    └─ profile_engine.py       indexed, in-memory lookup of any house
    └─ profile_assignment.py   seeded assignment of houses to grid slots
             │
             └─ dss_builder.py        grid model in OpenDSS language
             └─ normal_scenario.py    bind houses, solve, telemetry
             │
             └─ network/normal.py    SCADA round-trip -> network-events CSV
             │
             └─ attack/              six MITRE-ICS scenarios + physical deltas
```

---

## 1. Units

Three quantities appear throughout the pipeline; they are not interchangeable:

- **kWh** — *energy*: how much electricity a house used during a half-hour.
- **kW** — *power*: how much it draws at a given moment. For a half-hour
  window, average power = energy × 2.
- **kWp** — rated solar size: the capacity of the rooftop array at full sun
  (a static label, not a live measurement).

Each house's diary contains three row kinds: **GC** (usual household use),
**CL** (controlled load — e.g. off-peak hot water; only 139 of 300 homes have
it), and **GG** (solar generation). GC and GG exist for every house every day;
CL does not exist for the other 161 houses.

---

## 2. Step 1 — `grid_data/ausgrid_parser.py`

**What it does.** The raw Ausgrid CSV needs cleaning: a one-line preamble, no
row-quality column, dates like `1-Jul-10`. This script reads the file, skips
the junk, and produces one clean, validated record per row.

**Why it matters.** This is the only stage that touches the raw file, so its
checks (row counts: 109,500 GC, 50,735 CL, 109,500 GG; no missing values; every
value parses) certify everything downstream. 12,947,280 cells checked, no
surprises.

**Command:**

```bash
python3 grid_data/ausgrid_parser.py "Solar home 2010-2011.csv" --sample 5
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file (must be in the repo root) |
| `--sample N` | pretty-print the first N parsed rows for inspection |

**Output.** Nothing is written; records are yielded in memory for the next
stage.

---

## 3. Step 2 — `grid_data/ausgrid_timeseries.py`

**What it does.** The parser's rows are wide (one row = one house-day with 48
columns named `0:30`, `1:00`, … `0:00`). This script melts them into one row
per `(customer, category, 30-minute slot)` with a real timestamp —
12,947,280 rows.

**Why it matters.** Timestamps are the time axis of the simulation. The script
also checks the calendar for all 269,735 house-day groups: exactly 48 slots,
starting 00:00, ending 23:30, nothing missing or duplicated. It does **not**
convert kWh→kW; that happens in the next stage.

**Command:**

```bash
python3 grid_data/ausgrid_timeseries.py "Solar home 2010-2011.csv"
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file |
| `--no-write-csv` | validate only; write nothing |
| `--output PATH` | where to write (default `ausgrid_timeseries_2010_2011.csv`) |
| `--validate` | run the timestamp integrity checks and print a report |

---

## 4. Step 3 — `grid_data/ausgrid_profiles.py`

**What it does.** Merges, for every half-hour slot, `GC + CL` into **load**
and keeps `GG` as **solar**, converting energy to power (`kW = kWh × 2`).

**Why it matters.** The power-flow solver reads power, not energy, and needs
one timeline per house. Missing CL is fine (it becomes 0). The result is
**5,256,000** profiles: one per house per half-hour of the year, each with
`load_kw` and `solar_kw`.

**Command:**

```bash
python3 grid_data/ausgrid_profiles.py "Solar home 2010-2011.csv" --validate
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file |
| `--no-write-csv` | validate only; write nothing |
| `--output PATH` | destination (default `ausgrid_profiles_2010_2011.csv`, ~360 MB) |
| `--validate` | run the 11 integrity checks plus an energy cross-check against the source |

---

## 5. Step 4 — `grid_data/profile_engine.py`

**What it does.** A library, not a script: it loads the 360 MB profile file
once, builds an index, and then answers "give me house 58's whole year" in
O(1). It also answers `customer_has_cl(cid)` and `get_customer_metadata(cid)`.

**Why it matters.** Later stages must not re-parse 12 million rows every time
they need one house. One build, many fast lookups.

```python
engine = ProfileEngine("grid_data/ausgrid_profiles_2010_2011.csv")
engine.get_customer_profile(58)   # that house's 17,520 samples
```

---

## 6. Step 5 — `grid_data/profile_assignment.py`

**What it does.** A power grid has a fixed number of customer slots (25 for
ieee37, 85 for ieee123). This script chooses houses from the pool uniformly at
random, **without replacement**, seeded, and maps "slot 1 → house 58,
slot 2 → house 13, …". With `--output` it saves the mapping as
`profile_assignment.csv`.

**Why it matters.**

1. A fixed seed means the same mapping every run — this is what makes every
   experiment reproducible (`seed=42` comes from the original spec).
2. The assignment file is a stable handshake: the normal run and the attack
   run both read the *same* file, so they differ **only** by the attack.
   Load and solar stay together per house.

**Command:**

```bash
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 25 --seed 42 --output grid_data/profile_assignment_25_seed42.csv
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 85 --seed 42 --output grid_data/profile_assignment_85_seed42.csv
```

| Parameter | Meaning |
|---|---|
| `profile_csv` | the A-4 profiles file (used for metadata only) |
| `--count N` | number of slots to fill (25 for ieee37, 85 for ieee123) |
| `--seed N` | reproducible selection seed (default 42) |
| `--validate` | run the 10 assignment checks and print |
| `--detail N` | print the first N assignments (default 5) |
| `--output PATH` | export the assignment as CSV (Phase B-8); without it, nothing is written |

CSV columns are fixed: `assignment_id, customer_id, generator_capacity_kwp,
seed, cl_present`.

---

## 7. Step 6 — `simulator/power/dss_builder.py`

**What it does.** The grid model (topology, lines, transformers, loads) lives
in a neutral "Common Grid Model" format, which the OpenDSS power-flow engine
cannot read directly. This script translates the model into OpenDSS commands,
one `New LineCode`, `New Transformer`, `New Line`, `New Load` per real object.

**Why it matters.** Physical decisions live here, and mistakes surface as
wrong voltages. The translation was verified against the official IEEE test
cases:

- ieee37: voltages match the official OpenDSS case almost exactly — same
  shape, with a uniform −6.5 % offset that equals the official voltage
  regulator tap (we hold regulators at neutral — a deliberate, documented
  choice).
- ieee123: matches except a −0.7 %…−10 % offset from the real 115/4.16 kV
  substation transformer that the model declares but the official case
  abstracts away.

Three real bugs were caught and fixed during validation (transformer wiring,
a connection label, and a matrix-format quirk).

---

## 8. Step 7 — `simulator/power/normal_scenario.py`

**What it does.** Runs the simulation once per half-hour step:

1. Load the feeder (ieee37 or ieee123) and translate it (step 6).
2. Read the assignment file (step 5) and bind houses to slots **in file order**.
3. For each timestamp, scale a house's *rated* grid load by its real profile
   and solve the power flow: `kw_now = rated_kw × (profile_kw_now / average)`.
   The grid keeps its designed size; the daily shape comes from real people.
4. Record the result and move to the next timestamp.

**Why it matters.** This is the honest, no-attack baseline. Every voltage and
power value it writes is the reference that the attack layer later corrupts or
is measured against.

**Command:**

```bash
python3 -m simulator.power.normal_scenario \
    --feeder ieee37  --assignment grid_data/profile_assignment_25_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee37  --verbose
python3 -m simulator.power.normal_scenario \
    --feeder ieee123 --assignment grid_data/profile_assignment_85_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee123 --verbose
```

| Parameter | Meaning |
|---|---|
| `--feeder` | which grid: `ieee37` or `ieee123` |
| `--assignment PATH` | the B-8 seating chart (`profile_assignment_*.csv`) |
| `--profiles PATH` | the A-4 profile file (optional; defaults to `grid_data/ausgrid_profiles_2010_2011.csv`) |
| `--start` | first timestamp, ISO; default `2010-07-01T00:00:00` |
| `--days N` | number of days to simulate (default 7; `365` = full year ≈ 58 min) |
| `--out DIR` | where telemetry lands (creates `results/<feeder>/…`) |
| `--verbose` | print progress per day |

**Outputs** (all under `results/<feeder>/`):

| File | Contents |
|---|---|
| `normal_<feeder>_bus_telemetry.csv` | one row per `(timestamp, bus)` — line-to-line voltage per phase, in per unit of that bus's own voltage |
| `normal_<feeder>_current_telemetry.csv` | one row per `(timestamp, element, phase)` — RMS current (amps) **into** every line/switch at its near-end bus, plus the `Vsource` at the substation |
| `normal_<feeder>_summary.csv` | one row per step — feeder power (`source_p_kw`), solve convergence, min/max voltage, de-energized bus count |
| `normal_<feeder>_manifest.json` | run recipe — schema, binding, excluded buses, load model |

**Expected results** (7-day runs: 336 steps, per-element phase currents):

| feeder | solved | NaN readings | energized voltage range | feeder power | de-energized buses |
|---|---|---|---|---|---|
| ieee37 | 336/336 | 0 | 0.64 – 0.99 pu | 0.9–6.2 MW | 0 |
| ieee123 | 336/336 | 0 | 0.74 – 1.01 pu | 1.5–6.9 MW | 0 |

Every step converges and nothing is invalid. The low-voltage dips occur at the
rare moments when all assigned houses peak at once — a deliberately pessimistic
worst case, because these test grids are heavily loaded (see
`phase_d_report.md` §5a for the full audit).

---

## 9. Step 8 — `simulator/network/normal.py`

**What it does.** Two parties appear: `SCADA_MASTER` (the operator station)
and `RTU_<feeder>` (the field device on the grid). For every half-hour slot
the same exchange repeats:

1. **POLL** — master → RTU: no payload.
2. **TELEMETRY** — RTU → master: one message per measurement — every bus
   voltage, every element current, and the feeder summary — carrying the exact
   value the normal run wrote.

The script reads the telemetry CSVs from step 7 (never rewrites them), merges
the three files timestamp-by-timestamp, and turns each row into one message
with a deterministic identity (`normal_<feeder>-ev<NNNNNNNN>`).
`protocol="DNP3"` is a label for style; no packets are built.

**Why it matters.** This layer is the trusted baseline: every TELEMETRY value
is copied verbatim from the physical solution, so network truth equals physical
truth — quality `GOOD`, delivered, zero latency. The attack layer (step 9)
assumes precisely this trust. Event identity is a pure function of the physics
(not of wall-clock or filesystem order), so the same grid produces the same
messages on any machine.

Point ids are built the same way for any feeder:

| Point id | Meaning |
|---|---|
| `BUS_<bus>_VPU_<AB\|BC\|CA>` | line-to-line voltage at a bus, per unit |
| `CURRENT_<type>_<element>_<A\|B\|C>` | RMS current into a line/switch at its near end, amps |
| `FEEDER_SOURCE_P` / `SOURCE_Q` / `VPU_MIN` / `VPU_MAX` / `CONVERGED` | feeder-head power, min/max voltage, solve flag |

**Command:**

```bash
python3 -m simulator.network.normal --feeder ieee37  --results-dir results/ieee37
python3 -m simulator.network.normal --feeder ieee123 --results-dir results/ieee123
```

| Parameter | Meaning |
|---|---|
| `--feeder` | which grid (`ieee37` / `ieee123`) |
| `--results-dir` | where the step-7 telemetry and manifest live (`results/<feeder>/`) |
| `--out` | optional output path (default `<results-dir>/normal_<feeder>_network_events.csv`) |

**Output:** `normal_<feeder>_network_events.csv` — 16 fixed, documented columns
(`event_id, scenario_id, timestamp, feeder_id, src_device, dst_device,
protocol, message_type, direction, sequence, point_id, value, unit, quality,
delivery_status, latency_ms`).

**Expected results** (7-day runs: one POLL plus its TELEMETRYs per step):

| feeder | events | polls | telemetry (bus / current / summary) |
|---|---|---|---|
| ieee37 | 74,592 | 336 | 36,288 / 36,288 / 1,680 |
| ieee123 | 156,576 | 336 | 67,536 / 87,024 / 1,680 |

Every value equals the telemetry row it came from — for example, bus 718's
`vpu_AB` appears in the messages exactly as the solver wrote it.

---

## 10. Step 9 — `simulator/attack/`

**What it does.** The attack engine runs six scenarios against the *same* grid
model and the *same* network-event baseline, one scenario at a time. Each
scenario follows the same procedure:

1. **Inventory** — collect what is reachable: every measurement point from the
   step-8 CSV plus the controllable devices (switches, capacitors, loads,
   regulators) in the grid model.
2. **Preconditions** — if the scenario's prerequisites are not met, stop with a
   structured `FAILED` report and the reason.
3. **Target discovery** — filter the inventory by what the attack needs (a
   voltage point, a load coefficient, any switch…) and pick one
   deterministically with seed 42. **Targets are never hardcoded** — the engine
   works on any feeder.
4. **Execute** — produce the tampered state and measure its physical effect:
   the runner rebuilds the OpenDSS feeder and solves it twice (baseline vs.
   tampered), recording voltage/power deltas.
5. **Events** — emit the attack's network messages, labelled `injected=1`
   wherever the operator should not trust them.

**Why it matters.** Three design choices are worth noting:

- **Feeder-agnostic targeting.** The ieee37 grid has no modelled switches, so
  `unauthorized_command` falls back to a load; ieee123 has switches, so it
  selects one. Same code, different physics — no feeder-specific branches.
- **Fail-closed honesty.** No compatible target produces a structured `FAILED`
  result, and a forged report is never disguised as a genuine device echo.
  `injected`, `original_value` (physical truth) and `reported_value` (what the
  operator sees) make every event self-describing.
- **Traceability.** Attack rows carry the same feeder-scoped `scenario_id`
  (`<attack>_<feeder>`) as the normal rows, so each attack message can be
  linked to the exact telemetry it corrupts.

The six scenarios and their MITRE ATT&CK for ICS techniques:

| Scenario (`attack_id`) | MITRE technique | What the adversary does |
|---|---|---|
| `reconnaissance` | T0846 Remote System Discovery (Discovery) | enumerate measurement points and controllable devices |
| `unauthorized_command` | T0855 Unauthorized Command Message (Impair Process Control) | set a device to a forbidden state (e.g. open a closed switch) — real physical delta |
| `parameter_modification` | T0836 Modify Parameter (Impair Process Control) | scale a load's kvar multiplier outside its envelope — real physical delta |
| `false_measurement` | T0856 Spoof Reporting Message (Evasion) | report `truth × 1.05` — grid untouched; physical deltas are zero by construction |
| `communication_disruption` | T0804 Block Reporting Message (Inhibit Response Function) | take a truthful reading and never deliver it (`delivery_status=DROPPED`) |
| `multi_step_attack` | T0846 + T0855 + T0836 | one chronological timeline: discover → command → modify |

**Command:**

```bash
python3 -m simulator.attack --feeder ieee37              # all six scenarios
python3 -m simulator.attack --feeder ieee123 --attack parameter_modification
python3 -m simulator.attack --feeder ieee37 --seed 7 --modify-delta 0.50
```

| Parameter | Meaning |
|---|---|
| `--feeder` | which grid to attack (requires its step-8 CSV) |
| `--attack` | run one scenario only (default: all six) |
| `--seed` | deterministic target selection (default 42) |
| `--timestamp` | reference timestamp for selection (default `2010-07-01T00:00:00`) |
| `--modify-delta` | how far `parameter_modification` moves the coefficient (default 0.35) |

**Output.** Reports are printed only; persistence is handled by the next layer
(Task H produces the combined labelled CSV, ground truth and manifest; Task I
re-certifies it end to end — see `task_h_report.md` and
`phase_e_f_g_h_i_report.md`). The exit code is non-zero if any scenario FAILED.

---

## 11. Reproducing the pipeline

```bash
# 0. one-time: engine and dependencies (system Python; no venv in this environment)
pip3 install --user --break-system-packages "opendssdirect.py==0.9.4"

# 1. diary -> profiles (each writes its CSV; --validate for the reports)
python3 grid_data/ausgrid_parser.py "Solar home 2010-2011.csv"
python3 grid_data/ausgrid_timeseries.py "Solar home 2010-2011.csv"
python3 grid_data/ausgrid_profiles.py "Solar home 2010-2011.csv" --validate

# 2. seating charts (Phase B-7/B-8), shared by normal and attack runs
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 25 --seed 42 --output grid_data/profile_assignment_25_seed42.csv
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 85 --seed 42 --output grid_data/profile_assignment_85_seed42.csv

# 3. normal scenarios (Phase D) — telemetry in results/<feeder>/
python3 -m simulator.power.normal_scenario \
    --feeder ieee37  --assignment grid_data/profile_assignment_25_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee37  --verbose
python3 -m simulator.power.normal_scenario \
    --feeder ieee123 --assignment grid_data/profile_assignment_85_seed42.csv \
    --start 2010-07-01T00:00:00 --days 7 --out results/ieee123 --verbose

# 4. network events (Phase E) — the SCADA round-trip over the telemetry
python3 -m simulator.network.normal --feeder ieee37  --results-dir results/ieee37
python3 -m simulator.network.normal --feeder ieee123 --results-dir results/ieee123

# 5. attacks (Phase F/G) — all six MITRE-ICS scenarios; printed reports only
python3 -m simulator.attack --feeder ieee37
python3 -m simulator.attack --feeder ieee123

# 6. (optional) combined labelled dataset (Task H) + ground-truth validation (Task I)
python3 -m simulator.dataset --feeders ieee37,ieee123 \
    --events-dir results --results-dir results/dataset
python3 -m simulator.dataset --feeders ieee37,ieee123 \
    --events-dir results --results-dir results/dataset --validate-ground-truth

# 7. tests
python3 -m unittest -v simulator.power.test_dss_builder simulator.power.test_normal_scenario
RUN_E2E=1 python3 -m unittest -v simulator.power.test_e2e_scenario
python3 -m pytest -q            # 71 existing tests, unchanged
python3 -m pytest -q simulator/network simulator/attack simulator/dataset simulator/power
                                # E->H pipeline: 125 passed, 2 skipped
```

Sizes and time: `ausgrid_profiles_2010_2011.csv` is ~360 MB (materializing the
parser/timeseries outputs takes minutes); a full-year scenario run takes about
an hour per feeder, so use `--days 7` for experiments.

---

## 12. Why each stage exists

| Script | Role | If removed |
|---|---|---|
| `ausgrid_parser.py` | parse and certify the raw diary | untrustworthy numbers downstream |
| `ausgrid_timeseries.py` | give every reading a timestamp | no time axis to simulate |
| `ausgrid_profiles.py` | one merged load+solar timeline, in kW | the solver would need raw kWh merges |
| `profile_engine.py` | indexed lookup of any house | every stage re-parses 360 MB |
| `profile_assignment.py` | fixed, seeded seating chart | normal and attack runs would differ for the wrong reason |
| `dss_builder.py` | speak the solver's language correctly | wrong voltages (three bugs caught here) |
| `normal_scenario.py` | the honest baseline telemetry | nothing to compare attacks against |
| `network/normal.py` | turn telemetry into a SCADA conversation | no trusted message baseline to corrupt |
| `attack/` engine + scenarios | six MITRE-ICS adversaries with physical deltas | no cyber fingerprints to detect |
| `dataset/` + ground truth | one labelled CSV, re-certified end to end | no verifiable attack dataset for research |

The data stages (steps 1–5) are intentionally unaware of the grid; the grid
stages (steps 6–7) are unaware of Ausgrid; the network and attack layers
(steps 8–9) see only messages and devices. This separation lets each layer be
tested, reused and re-run independently — which is what makes the whole
pipeline reproducible, one command per step.