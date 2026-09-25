# Workflow — From Electricity Diaries to Power-Flow Telemetry

*Read this if you want to understand the whole pipeline in one sitting. It's
written the Feynman way: plain words, small steps, and "why it matters" at each
stage. The technical reports live in `phase_d_report.md` and
`phase_e_f_g_h_i_report.md`; this file is the friendly map.*

---

## 0. The Story in One Paragraph

Some real Australian houses ("solar home" customers) journaled their
electricity use every half hour for a whole year — 300 households, 365 days,
48 readings a day. We take that diary, turn it into one clean timeline per
house, hand-pick 25 of those houses for one test power grid and 85 for another,
then run a power-flow solver that asks: *"given these houses plugged into this
grid right now, what voltage does every pole show?"* We do that for every
half-hour of a week (or a year), and we write the answer down as CSV telemetry.

Why? So later we can run the same thing with **attacks** switched on, compare
the two, and catch the power-grid fingerprints of a hack. But a utility doesn't
*see* the grid — it sees messages over a network. So between the physics and
the adversary we add a pretend SCADA conversation: an operator station polls
the field gear, and every measurement comes back as one numbered message
(Step 8). Then the adversary shows up (Step 9) and forges or blocks some of
those messages, or quietly un-tunes a device — every tampered message is
labelled for the police report.

The whole chain reads left to right:

    Ausgrid "Solar home" CSV (raw diary)
        └─ ausgrid_parser.py       clean, checked rows
        └─ ausgrid_timeseries.py   one row per half-hour slot
        └─ ausgrid_profiles.py     one merged timeline per house (kW)
        └─ profile_engine.py       fast "phone book" lookup of houses
        └─ profile_assignment.py   assign houses to grid slots (seed 42)
                 │
                 └─ dss_builder.py        build the grid in OpenDSS language
                 └─ normal_scenario.py    bind houses + solve + telemetry
                                           (ieee37 with 25 houses, ieee123 with 85)
                 │
                 └─ network/normal.py     the SCADA round-trip (POLL, TELEMETRY)
                                          -> normal_<feeder>_network_events.csv
                 │
                 └─ attack/               six MITRE-ICS adversaries + physical deltas
                                          (no hardcoded targets; deterministic seed 42)

---

## 1. Ground Rules (Units, or "what's a house worth")

Three numbers walk into a bar, and they are **not** the same thing:

- **kWh** = *energy* — how much electricity a house *used during* a half-hour.
- **kW** = *power* — how much it *draws right now*. Over a half-hour window,
  average power = energy × 2 (because a half hour is half of an hour).
- **kWp** = rated solar size — how big the roof array is *at full sun* (a label,
  not a live measurement).

Each house's diary has three kinds of rows: **GC** (usual household use),
**CL** (controlled load — e.g. off-peak hot water; only 139 of 300 homes have
it), and **GG** (solar generation). GC and GG are present for every house every
day; CL simply doesn't exist for the other 161 houses.

---

## 2. Step 1 — `grid_data/ausgrid_parser.py` (the librarian)

**What it does.** The raw Ausgrid CSV is a little messy (a one-line preamble,
no row-quality column in this release, dates like `1-Jul-10`). This script
reads the whole file, skips the junk, and produces one clean, validated record
per row.

**Why it matters.** Garbage in, garbage out. This stage is the *only* place
that touches the raw file, so its checks (counts match: 109,500 GC, 50,735 CL,
109,500 GG; no NAs; every value parses) are what make everything downstream
trustworthy. 12,947,280 cells checked, zero surprises.

**Command:**

```bash
python3 grid_data/ausgrid_parser.py "Solar home 2010-2011.csv" --sample 5
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file (must be in the repo root) |
| `--sample N` | pretty-print the first N parsed rows so you can eyeball them |

**Output.** Nothing saved — the parser *yields* records in memory for the next
stage.

---

## 3. Step 2 — `grid_data/ausgrid_timeseries.py` (the calendar)

**What it does.** The parser's rows are *wide* (one row = one house-day with 48
separate columns named `0:30`, `1:00`, … `0:00`). This script *melts* them:
turns each column into its own row tagged with a real timestamp, so you get
one row per `(customer, category, 30-minute-slot)` — 12,947,280 rows.

**Why it matters.** Timestamps are the language of simulation. It also checks
``the calendar is sane for all 269,735 house-day groups: exactly 48 slots,
starts at 00:00, ends at 23:30, nothing missing or doubled. It does **not**
convert kWh→kW yet (that's the next stage's job).

**Command:**

```bash
python3 grid_data/ausgrid_timeseries.py "Solar home 2010-2011.csv"
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file |
| `--no-write-csv` | only validate; don't write the output file |
| `--output PATH` | where to write (default `ausgrid_timeseries_2010_2011.csv`) |
| `--validate` | also run the timestamp integrity checks and print a report |

---

## 4. Step 3 — `grid_data/ausgrid_profiles.py` (the house summary)

**What it does.** A house shouldn't have three separate diaries — it should have
one. This script merges, for every half-hour slot, `GC + CL` into **load** and
keeps `GG` as **solar**, and converts energy to power (`kW = kWh × 2`).

**Why it matters.** Simulation reads *power*, not energy, and it wants one
timeline per house. Missing CL is fine (becomes 0). The result is
**5,256,000** profiles: one per house per half-hour of the year, each with
`load_kw` and `solar_kw`.

**Command:**

```bash
python3 grid_data/ausgrid_profiles.py "Solar home 2010-2011.csv" --validate
```

| Parameter | Meaning |
|---|---|
| `csv_path` | the raw Ausgrid file |
| `--no-write-csv` | validate only, write nothing |
| `--output PATH` | destination (default `ausgrid_profiles_2010_2011.csv`, ~360 MB) |
| `--validate` | run the 11 integrity checks + energy cross-check against the source |

---

## 5. Step 4 — `grid_data/profile_engine.py` (the phone book)

**What it does.** A *library*, not a script: it loads the 360 MB profile file
once, builds an index, and then answers "give me house 58's whole year" in
O(1). Also answers `customer_has_cl(cid)` (does this house have a controlled
load?) and `get_customer_metadata(cid)`.

**Why it matters.** Later stages shouldn't re-parse 12 million rows every time
they need one house. One build, millions of fast lookups.

```python
engine = ProfileEngine("grid_data/ausgrid_profiles_2010_2011.csv")
engine.get_customer_profile(58)   # that house's 17,520 samples
```

---

## 6. Step 5 — `grid_data/profile_assignment.py` (the seat assigner)

**What it does.** A power grid has a fixed number of "customer slots" (25 for
ieee37, 85 for ieee123). This script picks houses out of the phone book
uniformly at random — **without replacement** and **with a fixed seed** — and
says "slot 1 gets house 58, slot 2 gets house 13, …". With `--output` it saves
that as `profile_assignment.csv`.

**Why it matters.** Two things:
1. Same seed ⇒ same seating chart, every time. That's what makes every
   experiment reproducible (`seed=42` came from the original spec).
2. The assignment file is a *stable handshake*: the normal run and the later
   attack run both read the *same* file, so they differ **only** by the attack,
   nothing else. Houses themselves are the unit — load *and* solar stay together.

**Command:**

```bash
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 25 --seed 42 --output grid_data/profile_assignment_25_seed42.csv
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 85 --seed 42 --output grid_data/profile_assignment_85_seed42.csv
```

| Parameter | Meaning |
|---|---|
| `profile_csv` | the A-4 profiles file (only used for metadata) |
| `--count N` | how many slots to fill (25 for ieee37, 85 for ieee123) |
| `--seed N` | reproducible lottery seed (default 42) |
| `--validate` | run the 10 assignment checks and print |
| `--detail N` | print the first N seatings (default 5) |
| `--output PATH` | **export** the seating chart as CSV (Phase B-8). Without it, nothing is written |

CSV columns are locked: `assignment_id, customer_id, generator_capacity_kwp,
seed, cl_present`.

---

## 7. Step 6 — `simulator/power/dss_builder.py` (the translator)

**What it does.** The grid model (feeder topology, lines, transformers, loads)
lives in a neutral "Common Grid Model" format. OpenDSS — the power-flow engine —
doesn't speak it. This script translates the model into OpenDSS "spoken"
commands, one `New LineCode`, `New Transformer`, `New Line`, `New Load` per
real object.

**Why it matters.** This is where the physics decisions live, and where mistakes
show up as wrong voltages. Getting it right was verified *against the official
IEEE test cases*:

- ieee37: voltages match the official OpenDSS case almost perfectly — same
  shape, just a uniform −6.5 % offset that is exactly the official voltage-
  regulator tap (we hold regulators at neutral, a deliberate, documented choice).
- ieee123: matches except a −0.7 %…−10 % offset from the real 115/4.16 kV
  substation transformer the model declares but the official case abstracts away.

Three real bugs were caught and fixed here while validating (wrong transformer
wiring, a bad connection label, and a matrix-format quirk) — proof this step
earned its validation.

---

## 8. Step 7 — `simulator/power/normal_scenario.py` (the day-runner)

**What it does.** Ties everything together, once per half-hour step:

1. Load the feeder (ieee37 or ieee123) and translate it (Step 6).
2. Read the assignment file (Step 5) and bind houses to slots **in file order**.
3. For each timestamp, rescale a house's *rated* grid load by its real profile
   and solve the power flow: `kw_now = rated_kw × (profile_kw_now / average)`.
   (The grid keeps its designed size; the *shape* of the day comes from real
   people.)
4. Record the result and move on.

**Why it matters.** This is the "normal world" baseline. Every voltage and
power value it writes is the *honest, no-attack* reference that attacks will be
compared against.

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
| `--profiles PATH` | the A-4 profile file (**optional**, defaults to `grid_data/ausgrid_profiles_2010_2011.csv`) |
| `--start` | first timestamp, ISO, default `2010-07-01T00:00:00` (start of the Ausgrid year) |
| `--days N` | how many days to simulate (default 7; `365` = full year ≈ 58 min) |
| `--out DIR` | where telemetry lands (creates `results/<feeder>/…`) |
| `--verbose` | print progress per day |

**Outputs** (all under `results/<feeder>/`):

| File | What's inside |
|---|---|
| `normal_<feeder>_bus_telemetry.csv` | one row per `(timestamp, bus)` — line-to-line voltage per phase, in *per-unit* of that bus's own voltage |
| `normal_<feeder>_current_telemetry.csv` | one row per `(timestamp, element, phase)` — RMS current (amps) flowing **into** every line/switch at its near-end bus, plus the `Vsource` at the substation (feeder-head current) |
| `normal_<feeder>_summary.csv` | one row per step — feeder power (`source_p_kw`), whether the solve **converged**, min/max voltage, de-energized bus count |
| `normal_<feeder>_manifest.json` | recipe of the run — schema, binding, excluded buses, load model |

**What "good" looks like** (7-day runs, all 336 steps, per-element phase
currents recorded):

| feeder | solved | NaN readings | energized voltage range | duty | de-energized buses |
|---|---|---|---|---|---|
| ieee37 | 336/336 | 0 | 0.64 – 0.99 pu | P 0.9–6.2 MW, 36k current rows | 0 |
| ieee123 | 336/336 | 0 | 0.74 – 1.01 pu | P 1.5–6.9 MW, 87k current rows | 0 |

Every step converged and nothing is invalid. (The low voltage dips happen at the
rare moments when *all* assigned houses peak at once — a deliberately
pessimistic worst case, since these test grids are heavily loaded; see
`phase_d_report.md` §5a for the full audit.)

---

## 9. Step 8 — `simulator/network/normal.py` (the radio chatter)

**What it does.** Two characters appear: `SCADA_MASTER` (the operator's
brain) and `RTU_<feeder>` (the field radio bolted to the grid). For every
half-hour slot the master does the same little dance:

1. **POLL** — master → RTU: *"talk to me."* No payload.
2. **TELEMETRY** — RTU → master: *one message per measurement* — every bus
   voltage, every element current, and the feeder summary — carrying the exact
   number the normal run wrote down.

This script reads the telemetry CSVs from Step 7 (never rewrites them), merges
the three files timestamp-by-timestamp, and turns each row into one of these
messages with a deterministic identity (`normal_<feeder>-ev<NNNNNNNN>`).
`protocol="DNP3"` is a label for style — no packets are ever built.

**Why it matters.** This layer is the *trust baseline*: every TELEMETRY value
is copied verbatim from the physical solution, so network truth equals
physical truth — quality `GOOD`, delivered, zero latency. Later, the adversary
(Step 9) presumes exactly this trust. And because event identity is a pure
function of the physics (not wall-clock or filesystem order), two machines
that solve the same grid produce the same messages.

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
| `--results-dir` | where the Step-7 telemetry + manifest live (`results/<feeder>/`) |
| `--out` | optional output path (default `<results-dir>/normal_<feeder>_network_events.csv`) |

**Output:** `normal_<feeder>_network_events.csv` — 16 fixed, documented columns
(`event_id, scenario_id, timestamp, feeder_id, src_device, dst_device,
protocol, message_type, direction, sequence, point_id, value, unit, quality,
delivery_status, latency_ms`).

**What "good" looks like** (7-day runs: one POLL + its TELEMETRYs per step):

| feeder | events | polls | telemetry (bus / current / summary) |
|---|---|---|---|
| ieee37 | 74,592 | 336 | 36,288 / 36,288 / 1,680 |
| ieee123 | 156,576 | 336 | 67,536 / 87,024 / 1,680 |

Each value equals the telemetry row it came from — e.g. bus 718's `vpu_AB`
shows up in the messages exactly as the solver wrote it.

---

## 10. Step 9 — `simulator/attack/` (the adversary)

**What it does.** The attack engine runs six scenarios against the *same*
grid model and the *same* network-event baseline, one scenario at a time.
Every scenario goes through the same ceremony:

1. **Inventory** — collect what's reachable: every measurement point from the
   Step-8 CSV plus the controllable devices (switches, capacitors, loads,
   regulators) in the grid model.
2. **Preconditions** — if the attack's prerequisites aren't met, stop with a
   structured `FAILED` report and the reason. Never guess.
3. **Target discovery** — filter the inventory by what the attack needs
   (a voltage point, a load coefficient, any switch…) and pick one
   deterministically with seed 42. **No target is ever hardcoded** — the
   engine works on any feeder.
4. **Execute** — do the dirty deed, then measure the physical footprint: the
   runner rebuilds the real OpenDSS feeder and solves it twice (untampered
   baseline vs. tampered), reporting honest voltage/power deltas.
5. **Events** — emit the attack's network messages, labelled with
   `injected=1` wherever the operator shouldn't trust them.

**Why it matters.** Three design decisions do the work:

- **Feeder-agnostic targeting.** The ieee37 grid has no modelled switches, so
  `unauthorized_command` falls back to a load; ieee123 has switches, so it
  picks one. Same code, different physics — no `if feeder == ...` anywhere.
- **Fail-closed honesty.** No compatible target ⇒ a structured `FAILED` result,
  and a forged report is never disguised as a genuine device echo.
  `injected`, `original_value` (physical truth) and `reported_value` (what the
  operator sees) make each event self-describing.
- **Traceability.** Attack rows carry the same feeder-scoped `scenario_id`
  (`<attack>_<feeder>`) as the normal rows, so an analyst can pin each attack
  message to the exact telemetry it corrupts.

The six scenarios and their MITRE ATT&CK for ICS techniques:

| Scenario (`attack_id`) | MITRE technique | What the adversary does |
|---|---|---|
| `reconnaissance` | T0846 Remote System Discovery (Discovery) | probe the RTU; list every point + device (huge message spike — 274 + 641 events) |
| `unauthorized_command` | T0855 Unauthorized Command Message (Impair Process Control) | flip a device to a forbidden state (open a closed switch) — real physical delta |
| `parameter_modification` | T0836 Modify Parameter (Impair Process Control) | scale a load's kvar multiplier outside the envelope — real physical delta |
| `false_measurement` | T0856 Spoof Reporting Message (Evasion) | report `truth × 1.05` — grid untouched, physical deltas honestly zero |
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
| `--feeder` | which grid to attack (needs its Step-8 CSV) |
| `--attack` | run one scenario only (default: all six) |
| `--seed` | deterministic target lottery (default 42) |
| `--timestamp` | reference timestamp for selection (default `2010-07-01T00:00:00`) |
| `--modify-delta` | how far `parameter_modification` pushes the coefficient (default 0.35) |

**Output.** Printed reports only — persistence is donated to the next layer
(Task H bundle: one combined labelled CSV + ground truth + manifest; Task I
re-certifies it end to end — see `task_h_report.md` and
`phase_e_f_g_h_i_report.md`). Exit code is non-zero if any scenario FAILED.

---

## 11. Reproduce the Whole Thing, Top to Bottom

```bash
# 0. one-time: engine + deps (system Python; venv isn't available in this env)
pip3 install --user --break-system-packages "opendssdirect.py==0.9.4"

# 1. data diary -> profiles (each writes its CSV; --validate for the reports)
python3 grid_data/ausgrid_parser.py "Solar home 2010-2011.csv"
python3 grid_data/ausgrid_timeseries.py "Solar home 2010-2011.csv"
python3 grid_data/ausgrid_profiles.py "Solar home 2010-2011.csv" --validate

# 2. seating charts (Phase B-7/B-8), reusable by normal + attack runs
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 25 --seed 42 --output grid_data/profile_assignment_25_seed42.csv
python3 grid_data/profile_assignment.py grid_data/ausgrid_profiles_2010_2011.csv \
    --count 85 --seed 42 --output grid_data/profile_assignment_85_seed42.csv

# 3. normal scenarios (Phase D) — telemetry lands in results/<feeder>/
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

# 7. tests (proves nothing regressed; ~4 min for the end-to-end one)
python3 -m unittest -v simulator.power.test_dss_builder simulator.power.test_normal_scenario
RUN_E2E=1 python3 -m unittest -v simulator.power.test_e2e_scenario
python3 -m pytest -q            # 71 existing tests, unchanged
python3 -m pytest -q simulator/network simulator/attack simulator/dataset simulator/power
                                # E->H pipeline: 125 passed, 2 skipped
```

Heads-up on sizes/time: `ausgrid_profiles_2010_2011.csv` is ~360 MB
(materializing the parser/timeseries outputs is the slow part — minutes);
a full-year scenario run takes ~1 hour per feeder, so use `--days 7` to play.

---

## 12. Why Each Stage Must Exist (significance cheat-sheet)

| Script | One-line job | If it vanished |
|---|---|---|
| `ausgrid_parser.py` | read + certify the raw diary | untrustworthy numbers downstream |
| `ausgrid_timeseries.py` | give every reading a timestamp | no time axis to simulate |
| `ausgrid_profiles.py` | one merged load+solar timeline, in kW | engine would need raw kWh merges |
| `profile_engine.py` | instant lookup of any house | every stage re-parses 360 MB |
| `profile_assignment.py` | fixed, seeded seating chart | normal vs attack runs would differ for the wrong reason |
| `dss_builder.py` | speak the solver's language, correctly | wrong voltages (proof: 3 bugs caught here) |
| `normal_scenario.py` | the honest baseline telemetry | nothing to compare attacks against |
| `network/normal.py` | turn the telemetry into a SCADA conversation | no trusted message baseline to corrupt |
| `attack/` engine + scenarios | six MITRE-ICS adversaries, real physical deltas | no cyber fingerprints to detect |
| `dataset/` + ground truth | one labelled CSV, re-certified end to end | no verifiable attack dataset for research |

The data stages (Steps 1–5) know **nothing** about power grids on purpose —
a house diary doesn't care which pole it's wired to. The grid stages (Steps 6–7)
know **nothing** about Ausgrid. The network and attack layers (Steps 8–9) know
**nothing** about either: they only see messages and devices. That separation
is what lets each half be reused, re-tested, and re-run independently, and it's
the secret to reproducing the whole thing from one command per step.