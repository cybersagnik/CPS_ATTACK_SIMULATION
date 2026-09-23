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

This repository currently implements only **DATA PARSING**: turning the raw
Ausgrid "Solar home" CSV into validated, structured records that later stages
(profile processing, feeder assignment, simulation, attack engine) can consume.
The parser deliberately has **no knowledge** of feeders, buses, SCADA,
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
    ausgrid_parser.py                <-- IMPLEMENTED NOW
        |
        v
    structured records (this repo)
        |
        v
    [future Phase A-3]
    timestamp-based profile format   <-- future
        |
        v
    [future]
    feeder assignment                <-- future (teammate's feeder model)
        |
        v
    [future]
    normal simulation                <-- future
        |
        v
    [future]
    attack engine                    <-- future (teammate's MITRE stage)

Only the first box is implemented in this step. Everything below
"structured records" is deliberately **not** implemented here.

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
  belongs to Phase A Step 3, not this parser. Keeping raw kWh here means the
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
- **Wide format is currently preserved**: records keep one row per
  (customer, category, date) with 48 ordered interval values, matching the
  original CSV layout. This makes the parse step a lossless, easy-to-verify
  transformation.
- **Timestamp expansion is deferred**: melting the wide rows into a long
  timestamp-indexed profile format (one row per half-hour instant) changes the
  data model, requires deciding interval-start vs interval-end labelling, and
  is explicitly Phase A Step 3 — so it is intentionally not done here.
- **Errors are collected, not silently skipped**: any malformed row raises a
  single `AusgridValidationError` listing every problem with its CSV line
  number, so partial or corrupt files fail loudly rather than producing a
  quietly incomplete dataset.
- **No external dependencies**: only the Python standard library
  (`csv`, `datetime`, `re`, `dataclasses`, `argparse`) is used, keeping the
  parser easy to run and review.

## 9. Future Integration (Phase A Step 3+)

Phase A Step 3 will consume this parser directly:

```python
parser = AusgridParser("Solar home 2010-2011.csv")
records = parser.parse()
# -> feed records into the profile-processor that melts each record's
#    48 interval values into timestamp-based energy/power series,
#    then assign those profiles to feeder buses (teammate's feeder model),
#    then run the normal simulation, then the attack engine.
```

The contract Phase A Step 3 can rely on:
- one `AusgridRecord` per (customer, category, date), with `date` already a
  real `datetime.date`;
- `intervals` ordered exactly as in the CSV, each value a validated non-negative
  float in kWh;
- `row_quality` already normalised: `"actual"`/`"NA"` when the column exists,
  or `None` when the source file has no Row Quality column (unavailable);
- category labels guaranteed to be one of `GC`/`CL`/`GG`.

This file does **not** implement Step 3 or any later stage; it stops at
"structured records".
