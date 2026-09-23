# Feeder library

All models are the **official IEEE PES distribution test feeders** downloaded
from the working-group page:

`https://cmte.ieee.org/pes-testfeeders/resources/`

Each directory below maps to one downloaded ZIP; source URLs and per-model
status are recorded in `models/feeders/registry.yaml` (the single source of
truth).  The files are stored **unmodified** — this library is read-only input
to the simulation, never edited.

## Status legend

| status | meaning |
| --- | --- |
| `native_dss` | ships a runnable OpenDSS master script → usable by the pipeline now |
| `csv_model` | ships a structured CSV model (importable into OpenDSS) |
| `spec_data` | specification data only (XLS/Word/TCW/TXT); an OpenDSS model must be authored first |

## Inventory

| dir | feeder | format | status |
| --- | --- | --- | --- |
| `yyd/` | YYD 4-node (three-winding transform) | OpenDSS | `native_dss` — **active** |
| `ieee4_source/` | Standard 4-bus cases | Word | `spec_data` |
| `ieee4_4wire/` | 4-wire delta / wye-delta center-tapped | Word | `spec_data` |
| `ieee13_source/` | 13-bus (4.16 kV, unbalanced, regulator) | XLS/TCW/Word | `spec_data` |
| `ieee34/` | 34-bus (24.9 kV, long/light, regulators) | XLS/TCW/Word | `spec_data` |
| `ieee37/` | 37-bus (4.8 kV, delta underground) | XLS/TCW/Word | `spec_data` |
| `ieee123/` | 123-bus (4.16 kV, regulators + capacitors) | XLS/TCW/Word | `spec_data` |
| `ieee8500/` | 8500-node (MV+LV, ~4800 buses) | CSV (nested zips) | `csv_model` |
| `nev/` | Neutral-Earth-Voltage | XLS/PDF | `spec_data` |
| `short_circuit/` | Short-circuit cases (13/34/37/123-bus) | TXT | `spec_data` |
| `lvnts/` | 342-node low-voltage network | CSV | `csv_model` |
| `european_lv/` | European LV (+100 load profiles) | CSV + solution XLSX | `csv_model` |
| `comprehensive/` | Comprehensive test feeder | XLS + results | `spec_data` |

## Operational notes

* Only `yyd` can run the pipeline end-to-end today.  Its model is unchanged;
  the DSS wrapper (`simulator/power/dss_engine.py`) strips side-effecting
  `Show`/`Export` lines and forces a static Snap solve.
* The other native IEEE feeders (13/34/37/123) are specification datasets: the
  1991/1992 papers define buses, phase-configs, loads, regulators and
  transformers in XLS/TCW form.  Authoring OpenDSS scripts is the deferred
  IEEE-conversion phase; see `docs/DECISIONS.md` (D-008).
* `csv_model` feeders (8500-node, LVNTS, European LV) can be imported into
  OpenDSS via its CSV-import capability; the European feeder additionally
  ships OpenDSS *solution* workbooks (not model scripts) under `Solutions/`.
* `scripts/ingest_feeder.py` automates the path for any feeder that *does*
  ship a DSS master: extract → discover master script → validate solve →
  emit `models/mappings/<id>_topology.json` → register.
* Scenario/attack config is per-feeder: when running a new feeder, write
  `config/attacks/*.yaml` targeting that feeder's buses/registers.