# Workflow — From Electricity Diaries to Power-Flow Telemetry

*Read this if you want to understand the whole pipeline in one sitting. It's
written the Feynman way: plain words, small steps, and "why it matters" at each
stage. The technical report for Phase D lives in `phase_d_report.md`; this file
is the friendly map.*

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
the two, and catch the power-grid fingerprints of a hack.

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
                 └─ (later) attack engine + attacked simulation

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
| `normal_<feeder>_summary.csv` | one row per step — feeder power (`source_p_kw`), whether the solve **converged**, min/max voltage, de-energized bus count |
| `normal_<feeder>_manifest.json` | recipe of the run — schema, binding, excluded buses, load model |

**What "good" looks like** (7-day runs, all 336 steps):

| feeder | solved | NaN readings | energized voltage range | duty |
|---|---|---|---|---|
| ieee37 | 336/336 | 0 | 0.64 – 0.99 pu | P 0.9–6.2 MW |
| ieee123 | 336/336 | 0 | 0.74 – 1.01 pu | P 1.5–6.9 MW |

Every step converged and nothing is blank. (The low voltage dips happen at the
rare moments when *all* assigned houses peak at once — a deliberately
pessimistic worst case, since these test grids are heavily loaded.)

---

## 9. Reproduce the Whole Thing, Top to Bottom

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

# 4. tests (proves nothing regressed; ~4 min for the end-to-end one)
python3 -m unittest -v simulator.power.test_dss_builder simulator.power.test_normal_scenario
RUN_E2E=1 python3 -m unittest -v simulator.power.test_e2e_scenario
python3 -m pytest -q            # 71 existing tests, unchanged
```

Heads-up on sizes/time: `ausgrid_profiles_2010_2011.csv` is ~360 MB
(materializing the parser/timeseries outputs is the slow part — minutes);
a full-year scenario run takes ~1 hour per feeder, so use `--days 7` to play.

---

## 10. Why Each Stage Must Exist (significance cheat-sheet)

| Script | One-line job | If it vanished |
|---|---|---|
| `ausgrid_parser.py` | read + certify the raw diary | untrustworthy numbers downstream |
| `ausgrid_timeseries.py` | give every reading a timestamp | no time axis to simulate |
| `ausgrid_profiles.py` | one merged load+solar timeline, in kW | engine would need raw kWh merges |
| `profile_engine.py` | instant lookup of any house | every stage re-parses 360 MB |
| `profile_assignment.py` | fixed, seeded seating chart | normal vs attack runs would differ for the wrong reason |
| `dss_builder.py` | speak the solver's language, correctly | wrong voltages (proof: 3 bugs caught here) |
| `normal_scenario.py` | the honest baseline telemetry | nothing to compare attacks against |

The data stages (Steps 1–5) know **nothing** about power grids on purpose —
a house diary doesn't care which pole it's wired to. The grid stages (Steps 6–7)
know nothing about Ausgrid. That separation is what lets each half be reused,
re-tested, and re-run independently, and it's the secret to reproducing the
whole thing from one command per step.