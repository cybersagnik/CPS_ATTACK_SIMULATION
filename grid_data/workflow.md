# Ausgrid Parser Workflow

## 1. Purpose

This parser is the first stage of the Cyber-Physical System (CPS) attack simulation
pipeline. The overall project pipeline is:

    Feeder/Grid Model + Ausgrid Data
                |
                v
        Common Grid Model
                |
                v
        Normal Simulation
                |
                v
        Attack Engine (MITRE scenarios)
                |
                v
        Attacked Simulation
                |
                v
        Cyber + Grid Dataset

This repository currently implements **DATA PARSING (Phase A-2)**,
**WIDE → TIMESTAMP TIME-SERIES CONVERSION (Phase A-3)**,
**PHYSICAL PROFILE PROCESSING (Phase A-4)**, the **ProfileEngine
customer-level access layer (Phase B-6)**, the **generic random assignment
(Phase B-7)**, and the **profile_assignment.csv export (Phase B-8)**: turning
the raw Ausgrid "Solar home" CSV into validated, structured records, then into
timestamp-based long-format time series, then into merged per-customer
load/solar physical profiles (with the kWh → kW conversion), then into
an indexed per-customer access layer, then into
customer-to-generic-slot random assignments, and finally into a persisted
assignment file that later stages (feeder integration, simulation, attack
engine) can consume.
The modules deliberately have **no knowledge** of feeders, buses, SCADA,
simulation, attacks, or MITRE — that keeps the stages decoupled and the data
path reproducible.

## 2. Dataset

- **Source CSV**: `Solar home 2010-2011.csv` (Ausgrid solar home electricity
  data, Aug 2014 release), located in the project root.
- **Time period**: 1 July 2010 → 30 June 2011 (365 days).
- **Customers**: 300 residential solar-home customers (IDs 1–300) across 100
  postcodes.
- **Consumption categories** (three row types per customer/day, where present):
  - `GC` — General Consumption (household consumption)
  - `CL` — Controlled Load Consumption (off-peak/controlled load)
  - `GG` — Gross Generation (solar generation, measured separately from loads)
- **48 half-hour interval columns** (`0:30` … `0:00`) containing **energy in
  kWh per interval**, not instantaneous kW. Example: `0:30` is the energy used
  between 00:00 and 00:30; `0:00` is the energy used between 23:30 and 00:00.
- **Row Quality**: per Ausgrid's notes, a blank Row Quality means all
  half-hour values are actual meter readings, and `NA` means some/all values
  are estimated/substitute.

### Facts discovered from the actual file (verified, not assumed)

- The file starts with a one-line preamble (a note referring to the attached
  Ausgrid PDF) before the real header. The parser skips exactly **1** preamble row.
- Header order is `Customer, Generator Capacity, Postcode, Consumption Category,
  date`, followed by 48 interval columns.
- **There is no `Row Quality` column in the 2010-2011 file.** The parser
  detects this and reports `Row Quality column present: False`. Because the
  column is absent, the parser does **not** infer or manufacture a value for
  every row — each record's `row_quality` is set to `None` (unavailable),
  and the validation summary reports `Row Quality values: unavailable`.
- There are **no `NA` values and no blank cells anywhere** in the file (verified
  over all 12,947,280 interval cells).
- Date format is `D-Mon-YY` (e.g. `1-Jul-10`), not `DDMMMYYYY`. The parser
  accepts both this and `DDMMMYYYY` (and ISO) for robustness across Ausgrid
  releases.
- Category row counts (observed): `GC` 109,500 · `CL` 50,735 · `GG` 109,500.
  `GC` and `GG` cover all 300 customers × 365 days; `CL` covers only **139**
  customers — not every home has a controlled load. This is real and expected,
  not a data error.
- Generator Capacity is constant per customer (62 distinct values, 1.0–9.99 kWp)
  and represents **rated solar capacity in kWp**, which is distinct from both
  instantaneous power (kW) and interval energy (kWh).
- All 48 interval values parse as non-negative floats; no duplicate
  (customer, category, date) rows exist; no malformed rows exist.

## 3. Project Position

    Ausgrid CSV
        |
        v
    ausgrid_parser.py                <-- Phase A-2 (implemented)
        |
        v
    structured source records
        |
        v
    ausgrid_timeseries.py            <-- Phase A-3 (implemented)
        |
        v
    timestamp-based time series
        |
        v
    ausgrid_profiles.py              <-- Phase A-4 (implemented)
        |
        v
    physical load/solar profiles (kWh -> kW, kWp metadata)
        |
        v
    profile_engine.py                 <-- Phase B-6 (implemented)
        |
        v
    customer-level profile access (indexed, 17,520 samples/customer)
        |
        v
    profile_assignment.py             <-- Phase B-7 (implemented, this doc)
        |
        v
    generic assignment slots (assignment_id -> customer_id, seed 42)
        |
        v
    profile_assignment.csv           <-- Phase B-8 (implemented, this doc)
        |
        v
    [future Phase C] feeder integration <-- future (Sagnik's feeder model)
        |
        v
    [future] normal simulation        <-- future
        |
        v
    [future] attack engine            <-- future (teammate's MITRE stage)

Phase A-2 (CSV parsing), Phase A-3 (wide → timestamp time series),
Phase A-4 (physical profile processing), Phase B-6 (ProfileEngine access
layer), Phase B-7 (generic random assignment) and Phase B-8
(profile_assignment.csv export) are implemented in this step. Everything below
"random assignment" is deliberately **not** implemented here (Phase C feeder
binding via the Common Grid Model is future work).

## 4. Parser Architecture

    input       "Solar home 2010-2011.csv"
                  |
    load        stdlib csv.reader (handles CRLF), skip preamble, locate header
                  |
    validate    required metadata columns present;
                48 interval columns detected from header;
                categories restricted to GC/CL/GG;
                Row Quality column presence detected
                  |
    parse       Customer / Postcode -> int
                Generator Capacity  -> float (kWp)
                Consumption Category-> str
                date                -> datetime.date
                48 intervals        -> float (kWh), order preserved
                Row Quality         -> "actual" / "NA" when the column exists,
                                      or None (unavailable) when it does not
                  |
    output      list[AusgridRecord]; malformed rows are collected and raised
                as a single AusgridValidationError (never silently dropped)

The parser is a standalone module with no third-party dependencies. Public
entry points are `AusgridParser(path).parse()` and `AusgridParser.summarize(records)`.
A CLI wrapper prints a validation summary.

## 5. Data Fields

Each parsed row becomes one `AusgridRecord` with:

| Field                    | Type            | Meaning                                             |
|--------------------------|-----------------|-----------------------------------------------------|
| `customer_id`            | `int`           | Ausgrid customer ID (1–300)                         |
| `postcode`               | `int`           | Customer postcode                                   |
| `generator_capacity_kwp` | `float`         | Rated solar capacity in **kWp** (preserved as-is)   |
| `category`               | `str`           | `GC` / `CL` / `GG`                                  |
| `date`                   | `datetime.date` | Calendar date of the 48 intervals                   |
| `row_quality`            | `str` or `None` | `"actual"`/`"NA"` when the column exists; `None` (unavailable) when the file has no Row Quality column |
| `intervals`              | `OrderedDict[str, float]` | `{interval_label: kWh}` in source column order |
| `line_number`            | `int` or `None` | 1-based physical line in the CSV (for traceability) |

`intervals` uses the CSV's own labels (`"0:30"` … `"0:00"`), and each value is
energy in **kWh** for that half-hour window — not kW.

## 6. Validation

Actual results from running the parser on the real `Solar home 2010-2011.csv`
(see Section 7 for the exact command):

```
Preamble rows skipped                 : 1
Row Quality column present            : False
Total parsed records                 : 269735
Unique customers                    : 300  (ids 1..300)
Date range                          : 2010-07-01 -> 2011-06-30  (365 days)
Interval columns (half-hour kWh)    : 48
Consumption categories              : {'GC': 109500, 'CL': 50735, 'GG': 109500}
  customers per category            : {'GC': 300, 'GG': 300, 'CL': 139}
Row Quality values                  : unavailable
Generator capacity kWp range        : 1.0..9.99  (62 distinct)
Distinct postcodes                  : 100
```

- CSV loaded successfully: yes (no parse errors).
- Expected columns detected: yes (Customer, Generator Capacity, Postcode,
  Consumption Category, date).
- 48 interval columns detected from the header: yes.
- Customer IDs parsed: yes (300 unique, 1..300).
- Dates parsed: yes (365 unique days covering 2010-07-01 → 2011-06-30).
- Categories recognized: yes (only GC / CL / GG).
- Interval values parsed: yes (all 12,947,280 cells are valid non-negative floats).
- Row Quality handled: yes. The column is detected when present (blank →
  `"actual"`, `"NA"` → `"NA"`). The real 2010-2011 file has **no** Row Quality
  column, so every record's `row_quality` is `None` and the summary reports
  `unavailable` — the parser does not infer a quality value that was never
  recorded.
- Malformed / unexpected rows: **0** invalid rows.
- Record count: 269,735 (= 269,737 total CSV lines − 1 preamble − 1 header).
- Category distribution: GC 109,500 · CL 50,735 · GG 109,500.
- Customer count: 300. Interval count: 48.

The parser's row-level error handling was additionally exercised with a
synthetic CSV containing deliberately bad rows (invalid category, invalid date,
missing interval value, non-numeric interval, bad capacity, bad postcode). All
six errors were reported in a single `AusgridValidationError` with line numbers,
confirming bad data is never silently ignored.

## 7. Execution

From the project root:

```bash
python ausgrid_parser.py "Solar home 2010-2011.csv"
```

Optionally print the first N parsed records:

```bash
python ausgrid_parser.py "Solar home 2010-2011.csv" --sample 3
```

As a module:

```python
from ausgrid_parser import AusgridParser

parser = AusgridParser("Solar home 2010-2011.csv")
records = parser.parse()          # list[AusgridRecord]
summary = parser.summarize(records)
```

## 8. Design Decisions

- **kWh is preserved**: the 48 columns are interval energy. Converting to kW
  (an average power over the interval) is a *profile-processing* concern and
  belongs to Phase A-4, not this parser. Keeping raw kWh here means the
  original Ausgrid semantics remain untouched for downstream stages.
- **kWp is preserved**: `generator_capacity_kwp` keeps the rated capacity and
  its unit in the field name, so it can never be confused with kWh (interval
  energy) or kW (instantaneous power).
- **Row Quality is preserved**: when the column exists, values are carried
  through as `"actual"`/`"NA"` (blank reads as `"actual"` per the Ausgrid
  notes). When the column does not exist (the real 2010-2011 file), every
  record's `row_quality` is `None` and the summary reports
  `Row Quality values: unavailable` — the parser does **not** manufacture an
  `"actual"` value for rows whose quality was never recorded.
- **Categories are preserved**: `GC`/`CL`/`GG` are kept verbatim with their
  full meanings documented; rows are not merged or re-labelled, so consumption
  vs. generation semantics survive into later stages.
- **Wide format is preserved by this module**: `ausgrid_parser.py` keeps one
  row per (customer, category, date) with 48 ordered interval values, matching
  the original CSV layout. This makes the parse step a lossless,
  easy-to-verify transformation.
- **Timestamp expansion lives in a separate module**: melting the wide rows
  into a long timestamp-indexed format changes the data model and requires
  deciding interval-start vs interval-end labelling, so it is done by
  `ausgrid_timeseries.py` (Phase A-3) rather than inside this parser.
- **Errors are collected, not silently skipped**: any malformed row raises a
  single `AusgridValidationError` listing every problem with its CSV line
  number, so partial or corrupt files fail loudly rather than producing a
  quietly incomplete dataset.
- **No external dependencies**: only the Python standard library
  (`csv`, `datetime`, `re`, `dataclasses`, `argparse`) is used, keeping the
  parser easy to run and review.

## 9. Future Integration (Phase A-3 onward)

Phase A-3 (wide → timestamp time series) now consumes this parser directly
via the separate `ausgrid_timeseries.py` module (documented in its own section
below). The contract Phase A-3/A-4 can rely on:
- one `AusgridRecord` per (customer, category, date), with `date` already a
  real `datetime.date`;
- `intervals` ordered exactly as in the CSV, each value a validated non-negative
  float in kWh;
- `row_quality` already normalised: `"actual"`/`"NA"` when the column exists,
  or `None` when the source file has no Row Quality column (unavailable);
- category labels guaranteed to be one of `GC`/`CL`/`GG`.

`ausgrid_parser.py` itself stops at "structured records"; Phase A-3 is
implemented separately in `ausgrid_timeseries.py` (next section).

## Phase A-3 — Wide CSV to Timestamp Time Series

### 1. Why this step exists

Phase A-2 produces one record per (customer, category, date) holding 48
half-hour energy values keyed by interval label. Downstream stages (kWh → kW,
profile assignment, simulation, attack engine) work in a timestamp-indexed
timeline, not in the Ausgrid wide layout. Phase A-3 reshapes the data into a
long time-series format — one record per half-hour timestamp — while leaving
every value, unit, and identifier untouched.

### 2. Why a separate module instead of expanding ausgrid_parser.py

`ausgrid_parser.py` is responsible for **CSV validation and structured source
records**. `ausgrid_timeseries.py` is responsible for **wide → timestamp
melting**. Keeping them separate means:

- parsing/validation stays independently reusable and testable;
- the converter reuses `AusgridParser` instead of duplicating CSV logic;
- neither module's API is coupled to the other's internal format;
- later stages can swap either stage independently.

The new module imports and calls `AusgridParser`; it does not re-implement
any parsing.

### 3. Input format

The output of Phase A-2: `list[AusgridRecord]`, one record per
(customer, category, date), with:

    customer_id, postcode, generator_capacity_kwp, category,
    date, row_quality, intervals = {label: kWh, ... 48 entries}

### 4. Output format

`list[AusgridTimeSeriesRecord]` (or a generator), one record per half-hour
sample:

    customer_id, postcode, generator_capacity_kwp, category,
    date, timestamp, energy_kwh, row_quality

The optional CSV export uses the same columns
(`ausgrid_timeseries_2010_2011.csv`).

### 5. Timestamp interpretation

The source interval labels mark the **end** of each half-hour window
(`0:30` = 00:00→00:30, `1:00` = 00:30→01:00, …, `0:00` = 23:30→00:00).
Each output `timestamp` is therefore the **start** of its interval, on the
source date:

    0:30  -> 00:00:00
    1:00  -> 00:30:00
    1:30  -> 01:00:00
    ...
    23:30 -> 23:00:00
    0:00  -> 23:30:00

The final `0:00` interval maps to **23:30 on the source date**, never to the
next day's 00:00. Exactly 48 timestamps are produced per source row, spaced
exactly 30 minutes apart.

### 6. Unit handling

**Phase A-3 does NOT convert kWh to kW.** Interval values remain energy in
`energy_kwh`, unchanged from the source. `generator_capacity_kwp` remains the
rated capacity in kWp. The kWh → kW conversion belongs to Phase A-4.

### 7. Row Quality handling

The 2010-2011 CSV has no Row Quality column, so the parser reports
`row_quality = None` and this module preserves it verbatim as `None` (null in
the output CSV). It never manufactures `"actual"`. The field stays in the
output schema so future datasets that include the column pass through
`"actual"`/`"NA"` unchanged.

### 8. Validation performed

- Source rows parsed: 269,735; output records = rows × 48.
- Total output count compared against `rows × intervals` (not hard-coded).
- Unique customers, categories, date range, timestamp range.
- Exactly 48 timestamps per source row (verified by count).
- Timestamp spacing: every consecutive pair within a source row is exactly
  30 minutes (full-stream check).
- First interval `0:30` → `00:00`; last interval `0:00` → `23:30`.
- Full energy round-trip: every one of the 12,947,280 output `energy_kwh`
  values compared against its source interval value (0 mismatches).
- No source records lost (output = 269735 × 48 exactly).
- Source CSV md5/mtime checked before and after (unmodified).
- Row Quality remains `None` everywhere (no `"actual"` manufactured).
- GC / CL / GG sample records printed.

### 9. Actual validation results

Run against the real `Solar home 2010-2011.csv`:

```
Source CSV                            : Solar home 2010-2011.csv
Parsed source rows                    : 269735
Interval columns                      : 48
Expected time-series records (rows x intervals): 12947280
Total time-series records             : 12947280
Unique customers                      : 300
Categories                            : {'GC': 5256000, 'CL': 2435280, 'GG': 5256000}
Date range                            : 2010-07-01 -> 2011-06-30
Timestamp range                       : 2010-07-01T00:00:00 -> 2011-06-30T23:30:00
Row Quality values                    : {None: 12947280}
```

Additional checks (run as a verification script):

- spacing exactly 30 min: True
- energy mismatch count: 0 of 12947280
- records == 269735 * 48: True
- 0:30 → 00:00:00, 1:00 → 00:30:00, 0:00 → 23:30:00

Category counts confirm CL's 139-customer subset: 50735 × 48 = 2,435,280.

#### Timestamp integrity validation

`AusgridTimeSeriesConverter.validate_timestamps(...)` performs a per-
(customer_id, category, date) integrity check on the generated time series:

  1. exactly 48 records exist;
  2. first timestamp is 00:00 on the source date;
  3. last timestamp is 23:30 on the source date;
  4. consecutive timestamps are exactly 30 minutes apart;
  5. no duplicate timestamps exist;
  6. no timestamp falls outside the source date.

It is implemented as a streaming single pass (constant per-group state) and
does not materialise the long-format output. It also does not change timestamp
semantics and performs no kWh → kW conversion.

Run on the real `Solar home 2010-2011.csv` (all checks verified by execution):

```
Timestamp integrity validation:
  [OK  ] exactly_48_records
  [OK  ] first_timestamp_0000
  [OK  ] last_timestamp_2330
  [OK  ] spacing_30_min
  [OK  ] no_duplicate_timestamps
  [OK  ] timestamps_within_source_date
  groups checked              : 269735  (total records: 12947280)
  all checks passed           : True
```

The validator was additionally exercised against deliberately corrupted
synthetic sequences (timestamp not starting at 00:00, dropped/duplicated
timestamps, out-of-date timestamps) and each corruption was correctly detected.

### 10. Example input → output transformation

Input (Phase A-2 wide record, customer 1, GC, 1 Jul 2010):

    intervals = {"0:30": 0.303, "1:00": 0.471, ..., "0:00": 0.125}

Output samples:

    customer_id=1, category=GC, date=2010-07-01,
    timestamp=2010-07-01T00:00:00, energy_kwh=0.303   (from label 0:30)
    customer_id=1, category=GC, date=2010-07-01,
    timestamp=2010-07-01T00:30:00, energy_kwh=0.471   (from label 1:00)
    ...
    customer_id=1, category=GC, date=2010-07-01,
    timestamp=2010-07-01T23:30:00, energy_kwh=0.125   (from label 0:00)

### 11. Exact command used

```bash
python3 ausgrid_timeseries.py "Solar home 2010-2011.csv"
```

(Use `--no-write-csv` to validate without writing the output CSV. Use
`--validate` to also run the timestamp integrity validation and print its
summary.)

### 12. What Phase A-4 will do next

Phase A-4 (profile processing) consumes the Phase A-3 time series and
performs the kWh → kW conversion (average power over each 30-minute window),
merging the GC/CL/GG streams into one physical profile per customer and
timestamp (load vs solar kept separate). **Phase A-3 does NOT convert kWh to kW.**

## Phase A-4 — Physical Profile Processing

### 1. Why this step exists

Phase A-3 produces three parallel time-series streams (GC, CL, GG) that are
keyed by `(customer_id, category, timestamp)`. Simulation and attack stages
need a single timeline per customer where consumption and generation are
combined into one record per timestamp. Phase A-4 merges the three streams
into one **physical profile** per `(customer_id, timestamp)` and converts
interval **energy (kWh) → average power (kW)** using the known 30-minute
interval. The rated solar capacity (`generator_capacity_kwp`) travels through
untouched as metadata.

### 2. Why a separate module instead of expanding ausgrid_timeseries.py

`ausgrid_timeseries.py` is responsible for **wide → timestamp melting** and
must stay unit-pure (kWh, categories separate). Combining categories into
load/solar, deriving kW, and defining what to preserve for feeder assignment
is a *profile* decision that belongs to its own stage. Keeping them separate
means:

- the time-series stage remains independently re-usable and testable;
- Phase A-4 can be re-run/changed without touching parsing or melting;
- later stages read exactly one profile file per customer and timestamp.

The new module only consumes `AusgridTimeSeriesRecord` objects (or the
`AusgridTimeSeriesConverter`); it never re-parses the CSV and never re-derives
timestamps.

### 3. Input format

The output of Phase A-3: `list[AusgridTimeSeriesRecord]` (or stream), three
records per `(customer_id, timestamp)` for full-coverage customers:

    customer_id, postcode, generator_capacity_kwp, category,
    date, timestamp, energy_kwh, row_quality

with `category` in `{"GC", "CL", "GG"}` over 12,947,280 records.

### 4. Output format

`list[AusgridProfile]` (or generator), **one record per (customer_id,
timestamp)** = 5,256,000 records (300 customers × 365 days × 48):

    customer_id, postcode, generator_capacity_kwp, timestamp,
    gc_kwh, cl_kwh, gg_kwh, load_kwh, solar_kwh,
    load_kw, solar_kw, row_quality

The optional CSV export uses the same columns
(`ausgrid_profiles_2010_2011.csv`). Column order is stable and documented.

### 5. Profile merging semantics

For every `(customer_id, timestamp)`:

    gc_kwh     = GC  energy for that timestamp (0.0 if customer has no GC row)
    cl_kwh     = CL  energy for that timestamp (0.0 if customer has no CL row)
    gg_kwh     = GG  energy for that timestamp (0.0 if customer has no GG row)
    load_kwh   = gc_kwh + cl_kwh              (total consumption energy)
    solar_kwh  = gg_kwh                       (generation energy)
    load_kw    = load_kwh * 2                 (avg power over 30-min interval)
    solar_kw   = solar_kwh * 2

- GC and GG cover all 300 customers × 365 days; CL covers only 139 customers.
  Customers without CL get `cl_kwh = 0.0` **and their profiles are still
  emitted** — a missing category never drops a (customer, timestamp).
- The merge is streaming: only one (customer, date) block of 48 records is
  held in memory at a time (time series is grouped by customer/date/category
  in source order, so a customer's GC/CL/GG timestamps for a given date are
  contiguous).
- A duplicated sample within one category for the same timestamp, or an
  unexpected category label, raises `AusgridProfileError` (never silently
  ignored).

### 6. kWh → kW conversion

Each time-series sample is **energy in kWh** covering a half-hour interval.
Average power over the interval is `energy / 0.5 h`, i.e. **× 2**:

    load_kw  = load_kwh  * 2
    solar_kw = solar_kwh * 2

The conversion factor `KWH_TO_KW = 2.0` is a named constant. Original
`gc_kwh`/`cl_kwh`/`gg_kwh` values are preserved unchanged alongside the kW
derivations (kWh is never destroyed).

### 7. What is deliberately NOT done (scope limits)

- **No `net_load_kw`** — load and solar are kept as **separate** fields, so
  simulation/attack stages decide how they interact.
- **No feeder/bus/load IDs** — assignment to network nodes is Phase A-5
  (teammate's feeder model), including load-phase assignment and which phase a
  generator connects to.
- **No reactive power** — P/Q ratio or power-factor override rules are Phase A-5
  decisions.
- **No generator target logic** — solar injection is applied in Phase A-5 only
  when a generator target exists (IEEE-37 / IEEE-123 have zero generator
  targets and therefore zero solar injection there).
- **No timezone handling** — timestamps are naive local wall-clock datetimes.
- **No capping/normalisation of `generator_capacity_kwp`** — it is metadata
  only, carried through verbatim (1.0–9.99 kWp).
- No databases, web APIs, ML, power-flow calculations, or attack logic.

### 8. Row Quality handling

Preserved verbatim from the time series. The 2010-2011 CSV has no Row Quality
column, so `row_quality = None` everywhere (never manufactured as `"actual"`).

### 9. `generator_capacity_kwp` handling

Carried through unchanged (rated solar capacity in kWp). It is metadata only:
not capped, not scaled to `solar_kw`, and not used in any calculation in this
phase.

### 10. Validation performed

`AusgridProfileConverter.validate_profiles(...)` performs a streaming,
per-`(customer_id, date)` integrity check over the generated profiles:

  1. exactly 48 profiles exist per (customer, date);
  2. first timestamp is 00:00 on the source date;
  3. last timestamp is 23:30 on the source date;
  4. consecutive timestamps are exactly 30 minutes apart;
  5. no duplicate timestamps within a group;
  6. no timestamp falls outside the source date;
  7. no duplicate (customer_id, timestamp) across the whole stream;
  8. `load_kwh == gc_kwh + cl_kwh` for every profile;
  9. `solar_kwh == gg_kwh` for every profile;
 10. `load_kw == load_kwh * 2` for every profile;
 11. `solar_kw == solar_kwh * 2` for every profile.

Plus CLI-level checks (single streaming pass):

- expected profile count derived from data: unique `(customer, date)` groups
  (= GC rows, since GC covers all customers/days) × 48, not hard-coded;
- aggregate identities on totals:
  `total_load_kwh == total_gc_kwh + total_cl_kwh`,
  `total_solar_kwh == total_gg_kwh`,
  `total_load_kw == total_load_kwh * 2`,
  `total_solar_kw == total_solar_kwh * 2`;
- category-energy cross-check (`--validate`): GC/CL/GG energy sums summed
  directly from the source time series must equal `total_gc_kwh` /
  `total_cl_kwh` / `total_gg_kwh` (proves no sample was lost or modified).

The validator was also exercised against deliberately corrupted synthetic
sequences (gaps, duplicated buckets, out-of-order groups) and each corruption
was detected.

### 11. Actual validation results

Run against the real `Solar home 2010-2011.csv`:

```
Source CSV                            : Solar home 2010-2011.csv
Parsed source rows                    : 269735
Interval columns                      : 48
Source time-series records (rows x intervals): 12947280
Expected profiles (unique customer+date x 48): 5256000
Total profile records                 : 5256000
Unique customers                      : 300
Date range                            : 2010-07-01 -> 2011-06-30
Timestamp range                       : 2010-07-01T00:00:00 -> 2011-06-30T23:30:00
Categories observed (source)          : {'GC': 109500, 'CL': 50735, 'GG': 109500}
Customers with CL (controlled load)   : 139
Customers with GG (gross generation)  : 300
Generator capacity kWp range          : 1.0..9.99
Row Quality values                    : {None: 5256000}

Totals (verified from generated profiles):
  total_gc_kwh                        : 1828903.049
  total_cl_kwh                        : 264950.893
  total_gg_kwh                        : 635572.862
  total_load_kwh                      : 2093853.942
  total_solar_kwh                     : 635572.862
  total_load_kw                       : 4187707.884
  total_solar_kw                      : 1271145.724

Verification of identities (from totals):
  total_load_kwh == total_gc_kwh + total_cl_kwh : True
  total_solar_kwh == total_gg_kwh               : True
  total_load_kw  == total_load_kwh * 2          : True
  total_solar_kw == total_solar_kwh * 2         : True
```

`--validate` integrity report:

```
Profile integrity validation:
  [OK  ] exactly_48_profiles_per_customer_date
  [OK  ] first_timestamp_0000
  [OK  ] last_timestamp_2330
  [OK  ] spacing_30_min
  [OK  ] no_duplicate_timestamps
  [OK  ] timestamps_within_source_date
  [OK  ] no_duplicate_customer_timestamp
  [OK  ] load_kwh_equals_gc_plus_cl
  [OK  ] solar_kwh_equals_gg
  [OK  ] load_kw_equals_load_kwh_x2
  [OK  ] solar_kw_equals_solar_kwh_x2
  groups checked          : 109500  (total profiles: 5256000)
  all checks passed       : True

Category-energy cross-check (profiles vs source time series):
  source GC energy == total_gc_kwh : True  (1828903.049 vs 1828903.049)
  source CL energy == total_cl_kwh : True  (264950.893 vs 264950.893)
  source GG energy == total_gg_kwh : True  (635572.862 vs 635572.862)
```

### 12. Example — customer with GC + CL + GG present (customer 1, 1 Jul 2010)

```
{'customer_id': 1, 'postcode': 2076, 'generator_capacity_kwp': 3.78,
 'timestamp': '2010-07-01T00:00:00', 'gc_kwh': 0.303, 'cl_kwh': 1.25,
 'gg_kwh': 0.0, 'load_kwh': 1.553, 'solar_kwh': 0.0,
 'load_kw': 3.106, 'solar_kw': 0.0, 'row_quality': None}
```

(shows `load_kwh = gc_kwh + cl_kwh` and `load_kw = load_kwh * 2`, with GG=0
overnight).

### 13. Example — customer with CL absent (customer 11, 1 Jul 2010)

```
{'customer_id': 11, 'postcode': 2026, 'generator_capacity_kwp': 2.04,
 'timestamp': '2010-07-01T00:00:00', 'gc_kwh': 0.118, 'cl_kwh': 0.0,
 'gg_kwh': 0.0, 'load_kwh': 0.118, 'solar_kwh': 0.0,
 'load_kw': 0.236, 'solar_kw': 0.0, 'row_quality': None}
```

Whole-year check on the output CSV confirmed customer 11 has `cl_kwh = 0.0`
for every one of its 17,520 profiles (0 non-zero CL rows). The profile is
still emitted.

### 14. Exact command used

```bash
python3 ausgrid_profiles.py "Solar home 2010-2011.csv" --validate
```

(Use `--no-write-csv` to validate without writing the output CSV. Use
`--output PATH` to change the destination, default `ausgrid_profiles_2010_2011.csv`.)

### 15. Memory / streaming design

`iter_profiles()` is a generator. Because the time series is emitted grouped
by (customer, date) and the categories of a date are contiguous, the converter
keeps only one (customer, date) block — 48 timestamps across the GC/CL/GG
buckets — in memory at a time. `convert()` materialises all 5,256,000 profiles
(≈ 360 MB CSV, larger in RAM) and is documented as the heavier option.

### 16. Expected-count derivation (not hard-coded)

The expected profile count is derived from the parsed data: the number of
unique `(customer, date)` groups equals the number of GC source rows (GC
covers all 300 customers × 365 days = 109,500), and every group yields exactly
48 timestamps → `109500 × 48 = 5,256,000`.

### 17. Unit/documentation invariants

- `kWh` = interval **energy** (preserved in `gc_kwh/cl_kwh/gg_kwh`, and summed
  into `load_kwh/solar_kwh`).
- `kW` = **average power** over the interval = `kWh * 2` (named constant
  `KWH_TO_KW`), stored in `load_kw/solar_kw`.
- `kWp` = rated generator capacity (metadata only, `generator_capacity_kwp`).
- Timestamps are naive local wall-clock; no timezone attached (matches Phase A-3).

### 18. Output files

- `ausgrid_profiles_2010_2011.csv` (Phase A-4 output, 5,256,001 lines incl.
  header, ≈ 359 MB) — the single per-timestamp profile file later stages ingest.
- `ausgrid_profiles.py` (this stage).
- `Solar home 2010-2011.csv` and `ausgrid_timeseries_2010_2011.csv` are never
  overwritten.

### 19. What Phase B-7 does (implemented) and what Phase C will do next

Phase B-7 (random assignment, see the dedicated section below) consumes these
profiles via the ProfileEngine (Phase B-6) and assigns each customer uniformly
at random to a generic slot (`assignment_id -> customer_id`). Phase C (feeder
integration) will then combine the assignments with the teammate's **Common
Grid Model** (feeder topology, buses, load IDs, phases), including
load-phase assignment, reactive-power rules (P/Q ratio or power-factor
override), and solar injection only onto generator targets (zero targets on
IEEE-37 / IEEE-123 → zero solar there).
**Phase A-4 stores no feeder/bus knowledge.**

### 20. Summary

Phase A-4 is functionally complete and verified on the real dataset:
12,947,280 time-series records were merged into 5,256,000 physical profiles
(GC/CL/GG combined per customer+timestamp, kWh preserved, kW derived ×2, CL
missing customers handled), all 11 integrity checks passed, category-energy
cross-checks against the source passed, and no feeder/network/attack logic was
introduced.

End of Phase A (data-engineering) implementation steps A-1 → A-4 for the
Ausgrid side.

## Phase B-6 — ProfileEngine

### 1. Purpose

Phase B-6 provides customer-level access to the already-processed Ausgrid
physical profiles. It consumes the **output of Phase A-4**
(`ausgrid_profiles_2010_2011.csv`) rather than re-implementing A-2/A-3/A-4, and
presents a clean API that the Phase B-7 random-assignment logic can use to
obtain a customer's complete load/solar time series.

### 2. Input

- `ausgrid_profiles_2010_2011.csv` — the Phase A-4 output (5,256,001 lines
  incl. header, ≈ 359 MB), one row per `(customer_id, timestamp)` in
  (customer_id, date, timestamp) order.

### 3. Output / API

Public API (see `profile_engine.py`):

    engine = ProfileEngine("ausgrid_profiles_2010_2011.csv")
    ids     = engine.get_customer_ids()            # list[int], file order
    profile = engine.get_customer_profile(1)       # CustomerProfile | None
    for c in engine.iter_customer_profiles():  # generator, one customer at a time
        ...
    summary = engine.summary()                     # dict (metadata, no materialisation)
    report  = engine.validate()                    # dict (integrity checks, generic)

- `CustomerProfile` (frozen dataclass): `customer_id`, `postcode`,
  `generator_capacity_kwp`, and `samples: Tuple[ProfileSample, ...]` in
  ascending timestamp order.
- `ProfileSample` (frozen dataclass) retains every A-4 physical field:
  `timestamp, gc_kwh, cl_kwh, gg_kwh, load_kwh, solar_kwh, load_kw, solar_kw,
  row_quality`.
- B-7-friendly series helpers: `load_kw_series()`, `solar_kw_series()`,
  `load_kwh_series()`, `solar_kwh_series()`, `timestamp_series()`.
- Unknown customer ids return `None` (documented API contract: a clear empty
  result, not an exception).

### 4. Memory strategy

- `ProfileEngine.__init__` performs a **single streaming pass** that only
  builds a lightweight index: `customer_id -> (byte offset of first row, row
  count)`, `customer_id -> (postcode, generator_capacity_kwp)`, plus aggregate
  counters (totals, CL/GG presence, capacity range, timestamp endpoints).
- **No `ProfileSample`/`CustomerProfile` objects are created during init** —
  the 5,256,000 rows are never materialised.
- `get_customer_profile(customer_id)` seeks directly to that customer's byte
  block and parses only that customer's ~17,520 rows.
- `iter_customer_profiles()` streams the file once, materialising one
  customer at a time.
- Verified: init makes zero calls to the row parser (`_parse_row`).

### 5. Customer-level profile semantics

- 300 customers, each with 365 days × 48 intervals = **17,520 samples**,
  from `2010-07-01 00:00` to `2011-06-30 23:30`, 30-minute spacing.
- The expected per-customer count in `validate()` is **derived** from the
  customer's own timestamps: `unique dates × 48` (INTERVALS_PER_DAY), not
  hard-coded to 17,520.
- `samples_per_customer` in `summary()` is read from the index; for the real
  dataset it is the uniform value 17,520.

### 6. Validation

`ProfileEngine.validate()` is generic (not hard-coded to customer 1) and
checks over every customer:

1. expected A-4 header present (enforced in `__init__`);
2. customer ids valid and discoverable;
3. no duplicate `(customer_id, timestamp)`;
4. samples per customer matches `days × 48` derived from that customer's
   timestamps;
5. timestamps strictly increasing per customer;
6. consecutive timestamps exactly 30 minutes apart;
7. `load_kwh == gc_kwh + cl_kwh`;
8. `solar_kwh == gg_kwh`;
9. `load_kw == load_kwh * 2`;
10. `solar_kw == solar_kwh * 2`;
11. `generator_capacity_kwp` preserved (non-negative, per-block consistent);
12. `row_quality` preserved (consistent within each customer).

### 7. Summary

`summary()` returns metadata without materialising the dataset:

    total_profiles, unique_customers, customer_id_range,
    timestamp_start, timestamp_end, samples_per_customer,
    customers_with_cl, customers_with_gg, generator_capacity_range,
    total_load_kwh, total_solar_kwh

### 8. CLI commands

```bash
python3 profile_engine.py ausgrid_profiles_2010_2011.csv
python3 profile_engine.py ausgrid_profiles_2010_2011.csv --validate
python3 profile_engine.py ausgrid_profiles_2010_2011.csv --customer 1
```

### 9. Unit tests

`test_profile_engine.py` (stdlib `unittest`) uses small synthetic CSV
fixtures (3 customers × 2 days × 48 intervals) — it never scans the 359 MB
dataset. Coverage:

- A. init does not materialise rows (row-parser call count == 0);
- B. customer-id discovery;
- C/E. `get_customer_profile(1)` with the expected sample count;
- D. unknown customer id returns `None`;
- F. customer 11 retains `cl_kwh = 0.0` (no controlled load);
- G. timestamp ordering;
- H. 30-minute spacing;
- I. physical identities;
- J. summary totals (verified against fixture arithmetic).

Run with `python3 -m unittest test_profile_engine -v` → 12 tests, all pass.

### 10. Real-data validation results

Run against the real `ausgrid_profiles_2010_2011.csv`:

```
ProfileEngine summary
  total profiles           : 5256000
  unique customers         : 300
  customer_id_range        : 1..300
  timestamp_start          : 2010-07-01T00:00:00
  timestamp_end            : 2011-06-30T23:30:00
  samples_per_customer     : 17520
  customers_with_cl        : 136
  customers_with_gg        : 300
  generator_capacity_range : 1.0..9.99 kWp
  total_load_kwh           : 2093853.942
  total_solar_kwh          : 635572.862
```

Validation report (all checks passed):

```
  [OK  ] expected_header
  [OK  ] customer_ids_valid
  [OK  ] sample_count_matches_days_x_48
  [OK  ] no_duplicate_customer_timestamp
  [OK  ] timestamps_increasing_per_customer
  [OK  ] spacing_30_min
  [OK  ] load_kwh_equals_gc_plus_cl
  [OK  ] solar_kwh_equals_gg
  [OK  ] load_kw_equals_load_kwh_x2
  [OK  ] solar_kw_equals_solar_kwh_x2
  [OK  ] generator_capacity_kwp_preserved
  [OK  ] row_quality_preserved
  customers checked        : 300
  total profiles checked   : 5256000
  all checks passed        : True
```

- `customers_with_cl = 136`: ProfileEngine detects controlled load by presence
  of **non-zero** `cl_kwh` in the A-4 profiles. The parser-level category count
  was 139; customers 27, 37, 281 carry the CL category in the source but have
  all-zero `cl_kwh` across the whole year, so they are not counted here. This
  is a value-presence measure, not a source-category count, and is reported
  consistently.
- Totals match Phase A-4's published values exactly:
  `total_load_kwh = 2,093,853.942` and `total_solar_kwh = 635,572.862`.
- Customer 11 (no CL): 17,520 samples, all `cl_kwh = 0.0`.
- Customer 1: 17,520 samples from `2010-07-01T00:00:00` … `2011-06-30T23:30:00`.

### 11. How Phase B-7 will consume ProfileEngine

Phase B-7 (random assignment) will do roughly:

    engine = ProfileEngine("ausgrid_profiles_2010_2011.csv")
    customer_ids = engine.get_customer_ids()
    for cid in customer_ids:
        profile = engine.get_customer_profile(cid)
        load_kw  = profile.load_kw_series()
        solar_kw = profile.solar_kw_series()
        # ... assign profile.load_kw / profile.solar_kw to a feeder target ...
        # ... then write profile_assignment.csv (Phase B-8) ...

Phase B-7 needs no CSV/parsing/grouping knowledge — the engine hides file
access, row parsing and grouping behind `CustomerProfile`.

### 12. Scope confirmation

ProfileEngine adds **no** feeder/bus/load/generator ids, no phase allocation,
no `Load.kw_per_phase`, no reactive-power/power-factor/IEEE-37/IEEE-123 logic,
no Common Grid Model integration, no SCADA/telemetry/attack/MITRE logic, no
network simulation, and does **not** write `profile_assignment.csv`
(serialisation is Phase B-8's `profile_assignment.py` role, not the engine's).
A-2/A-3/A-4 modules and files (`ausgrid_parser.py`, `ausgrid_timeseries.py`,
`ausgrid_profiles.py`, the two CSV outputs) are unchanged.

**Project status: Phase B-8 COMPLETE. NEXT = Phase C (feeder binding).**

## Phase B-7 — Random Assignment

### 1. Purpose

Phase B-7 assigns whole Ausgrid customers to a set of **generic assignment
slots** via uniform random sampling **without replacement**, producing a list
of `ProfileAssignment` records that later phases (B-8, then Phase C feeder
binding) consume. One assignment unit is

    assignment_id -> customer_id

e.g. "assignment 1 -> customer 58". B-7 does **not** decide that a slot maps
to a specific feeder node — that is Phase C's responsibility (Sagnik's feeder
model).

### 2. Input

- `profile_engine.py` (Phase B-6) — the sole dependency. B-7 uses only
  `ProfileEngine`'s public API and **never** re-parses the raw Ausgrid source
  or the A-4 CSV.

### 3. Output / API

```
assigner     = RandomProfileAssigner(ProfileEngine("ausgrid_profiles_2010_2011.csv"))
assignments  = assigner.assign(assignment_count=25, seed=42)  # list[ProfileAssignment]
report       = assigner.validate_assignments(assignments, requested_count=25)
summary      = assigner.summary(assignments)                  # dict, no file writing
```

- `ProfileAssignment` (frozen dataclass), one per slot:
  `assignment_id, customer_id, generator_capacity_kwp, seed, cl_present`
  (plus `to_dict()`, used by the Phase B-8 CSV export).
- CLI: `python3 profile_assignment.py ausgrid_profiles_2010_2011.csv
  --count 25 --seed 42` (`--validate`, `--detail N`, and Phase B-8's
  `--output PATH` optional).
- B-7 itself does **not** write `profile_assignment.csv` unless the Phase B-8
  `--output` flag is supplied.

### 4. Randomization semantics

- Uniform random sampling **without replacement** over the full eligible pool
  (all 300 customers); every customer has equal probability.
- Deterministic dedicated generator `random.Random(seed)`, default `seed = 42`.
  Never uncontrolled global randomness.
- Same pool + same `assignment_count` + same seed ⇒ identical ordered
  assignment. Different seed normally differs. (`generic seed = 42` noted in
  B-6 section 11 stays valid.) Verified on real data: seed 42 twice produced
  byte-identical summaries; seed 99 differed.
- `assignment_count` range-checked: `0 <= count <= number of eligible
  customers`, else a clear `ProfileAssignmentError`.

### 5. Load + solar coupling

A customer is a complete household profile. When customer X is assigned, both
its load (GC+CL, possibly CL = 0) and solar (GG) belong to that same
assignment. B-7 never samples load from one customer and solar from another —
the record holds a single `customer_id`, and load/solar are always fetched
together from that customer.

### 6. CL handling

All customers are equally eligible. Customers without CL simply have
`cl_kwh = 0.0` across the year (`load = GC + 0`); there is **no** CL eligibility
rule (nothing limits CL-only customers, and no CL exclusion exists).

### 7. cl_present semantics

`cl_present = True` iff the selected customer's profiles contain at least one
**non-zero** `cl_kwh`, per `ProfileEngine.customer_has_cl()` (the documented
B-6 value-presence measure ⇒ 136 CL customers). It is metadata recorded on
each assignment for Phase C; it is **not** derived per assignment from slot
type (B-7 slots are generic).

### 8. Solar capacity

`generator_capacity_kwp` is **metadata only**: carried through unchanged, never
used for eligibility, never clamped/normalised.

### 9. Feeder-agnostic scope

B-7 has **no** knowledge of IEEE-37/IEEE-123, feeder/load/generator/bus ids,
phases, `kw_per_phase`, Common Grid Model, `get_load_targets()` /
`get_generator_targets()`, SCADA, simulation, attack logic, or MITRE. Records
contain only `assignment_id, customer_id, generator_capacity_kwp, seed,
cl_present`.

### 10. Validation (10 generic checks)

1. count matches requested count; 2. assignment ids unique; 3. assignment ids
sequential 1..N; 4. customer ids unique (no-replacement); 5. every customer
exists in ProfileEngine; 6. capacity matches engine metadata; 7. seed recorded
consistently on every record; 8. `cl_present` matches `customer_has_cl()`;
9. same seed ⇒ same ordered assignment (tested in unit tests, verified on real
data); 10. different seed ⇒ different assignment (tested, verified). Plus an
"all selected customers within eligible pool" guard.

### 11. Additive ProfileEngine accessors (Phase B-7)

Two additive, non-semantic accessors were added to `profile_engine.py` so B-7
reuses B-6's own init-time index instead of re-parsing customer blocks:

    meta = engine.get_customer_metadata(cid)   # (postcode, kWp) | None, O(1)
    has_cl = engine.customer_has_cl(cid)        # bool, O(1)

No existing ProfileEngine behavior or results changed (all 12 B-6 tests still
pass).

### 12. Unit tests

`test_profile_assignment.py` (17 tests) covers A–N from the plan on a small
synthetic 3-customer fixture: count=0 / 1 / N, count>N fails, negative count
fails, unique customers, sequential ids, seed reproducibility (same seed
identical, different seed differs), capacity preserved, `cl_present` correct,
CL-absent customers eligible, load/solar coupling (single customer id; no
separate load/solar customer fields), no feeder dependency, and no
feeder/bus/load/generator fields in records.

### 13. Real-data verification

```
Eligible customers        : 300   (also for count = 85)
```

- `--count 25 --seed 42`: 25 assignments, 25 unique customers, `cl_present`
  count 8, capacity range `1.0..3.78 kWp`.

            assignment  1 -> customer  58 (kWp=1.0,  cl_present=False)
            assignment  2 -> customer  13 (kWp=2.22, cl_present=False)
            assignment  3 -> customer 141 (kWp=3.0,  cl_present=False)
            assignment  4 -> customer 126 (kWp=1.1,  cl_present=True)
            assignment  5 -> customer 115 (kWp=1.8,  cl_present=False)

- `--count 85 --seed 42`: 85 assignments, 85 unique customers, `cl_present`
  count 35, capacity range `1.0..6.2 kWp`.
- `validate_assignments` all checks OK for both runs.
- Reproducibility: seed 42 run twice ⇒ identical output; seed 99 differs.

### 14. Scope confirmation / files

`profile_assignment.py` (new, Phase B-7) and `test_profile_assignment.py`
(new). `profile_engine.py` gained only the two additive accessors listed
above. A-2/A-3/A-4 files and outputs (`ausgrid_parser.py`,
`ausgrid_timeseries.py`, `ausgrid_profiles.py`, the CSV outputs) are unchanged.
No `profile_assignment.csv` is written in this phase.

**Phase B-7 COMPLETE. Phase B-8 COMPLETE. NEXT = Phase C (feeder binding).**

## Phase B-8 — profile_assignment.csv

### 1. Purpose

Phase B-8 is the **export/persistence layer** for Phase B-7. It writes the
validated `list[ProfileAssignment]` to `profile_assignment.csv` so that Normal
and Attack simulation later reuse the **same** assignment file with **no
re-randomization** between them. It is a pure exporter: it never re-runs the
assignment algorithm and never touches customer profiles.

The relationship:

    ProfileEngine
         ↓
    RandomProfileAssigner        (B-7: uniform, no-replacement, seed)
         ↓
    list[ProfileAssignment]
         ↓
    ProfileAssignmentExporter     (B-8: THIS phase)
         ↓
    profile_assignment.csv

### 2. Exact CSV schema (locked, column order fixed)

    assignment_id,customer_id,generator_capacity_kwp,seed,cl_present

- `assignment_id`, `customer_id`, `seed` — integers.
- `generator_capacity_kwp` — float (metadata, never modified).
- `cl_present` — boolean, written as `True` / `False` (never `1`/`0`).

Example rows (real data, count=25, seed=42 — matches the B-7 spec example exactly):

    assignment_id,customer_id,generator_capacity_kwp,seed,cl_present
    1,58,1.0,42,False
    2,13,2.22,42,False
    3,141,3.0,42,False
    4,126,1.1,42,True
    5,115,1.8,42,False

### 3. What the CSV does NOT contain

No full customer profiles (17,520 samples live only behind
`ProfileEngine.get_customer_profile(customer_id)`), no
`feeder_id/load_id/generator_id/bus_id/phase/kw_per_phase/timestamp`/load or
solar columns. B-8 is **assignment metadata only**; feeder binding is Phase C.

### 4. Export API

    exporter = ProfileAssignmentExporter()
    exporter.export(assignments, "profile_assignment.csv",
                    assigner=assigner, requested_count=25)   # atomic write
    report = exporter.validate_file("profile_assignment.csv", expected_count=25)

- `export()` validates **before** writing (structure + B-7 semantic
  validation via `RandomProfileAssigner.validate_assignments()`) and fails
  loudly rather than writing corrupt output. It never leaves a partial file:
  it writes to a temp sibling then `os.replace()` atomically, cleaning up on
  failure.
- `validate_file()` reads the CSV back and verifies the exact header, row
  count, sequential/unique assignment ids, unique customer ids, valid
  capacities, consistent seed, and boolean `cl_present` (no missing fields /
  unexpected columns).
- `read_file()` returns `(columns, parsed row dicts)` for verification.

### 5. Determinism / reproducibility

- B-7 already returns identical assignments for identical
  `(engine input, count, seed)`; B-8 adds nothing nondeterministic (no
  timestamps, UUIDs, or generated metadata; the seed is already in every row).
- Verified: exporting count=25/seed=42 twice produced **byte-identical** CSV;
  seed=99 produced different assignment content.
- Rows are written in ascending `assignment_id` order — exactly the B-7
  selection order.

### 6. CLI usage

    python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 25 --seed 42
    python3 profile_assignment.py ausgrid_profiles_2010_2011.csv --count 25 --seed 42 --output profile_assignment_25_seed42.csv

The `--output` flag triggers export. Without it, the existing B-7 CLI behavior
is preserved and **no file is written** (an existing assignment file is never
overwritten unexpectedly). The CLI refuses to overwrite the input profile CSV.
Error paths now exit non-zero (`sys.exit(main())`).

### 7. Unit tests

`test_profile_assignment_export.py` (22 tests) on a synthetic fixture covers:
correct header/column order (A/B), row count (C), assignment/customer IDs (D/E),
capacity (F), seed (G), cl_present (H), deterministic/reproducible output (I),
invalid data rejected incl. atomic no-partial-file (J), CSV read-back match (K),
no unexpected columns incl. tampered header (L), no duplicate assignments (M),
and an engine-backed end-to-end export.

### 8. Real-data validation

- `profile_assignment_25_seed42.csv` — 25 rows + header; ids `1..25`; 25
  unique customers; capacity range `1.0..3.78 kWp`; `cl_present` count 8;
  all `validate_file()` checks OK.
- `profile_assignment_85_seed42.csv` — 85 rows + header; ids `1..85`; 85
  unique customers; capacity range `1.0..6.2 kWp`; `cl_present` count 35;
  all `validate_file()` checks OK.
- Reproducibility: seed 42 export twice ⇒ byte-identical file; seed 99 ⇒
  different content.
- The 5-row sample above is the actual first five rows of the exported file.

### 9. Scope confirmation / files

`profile_assignment.py` gained `ProfileAssignmentExporter`, `ASSIGNMENT_COLUMNS`
and the `--output` CLI path (no semantics of B-7 changed; 17 B-7 tests still
pass). `test_profile_assignment_export.py` is new. A-2/A-3/A-4 files and the
source/profile CSVs are unchanged. No Phase C (feeder/bus/generator binding) is
introduced here.

**PHASE B COMPLETE. NEXT = PHASE C (feeder binding via the Common Grid Model).**
