# What Changed in This Update

*Author-facing summary of the latest push. Read this first if you are picking
up the repository for the first time, or want to know exactly what the latest
commit added. For the deep technical report on the dataset work, see
`task_h_report.md`.*

**Commit:** `0118cf8` — "Task H: deterministic combined attack dataset + Phase F/G engine"
**Previous commit:** `a067bc0` ("I am ded")
**Files touched:** 42 new/updated files, ~514,600 added lines
**Branch:** `main` (pushed to `origin`, https://github.com/cybersagnik/CPS_ATTACK_SIMULATION)

---

## 1. What is this update about?

This push delivers **two new simulation packages** plus the **final labelled
attack dataset** they produce. Put together, they complete the pipeline that
was designed in `README.md`:

```text
common grid model  ──►  normal simulation  ──►  network events  ──►  ATTACK ENGINE  ──►  combined labelled dataset
```

The three concrete deliverables are:

1. **`simulator/attack/`** — the MITRE ATT&CK-for-ICS attack engine
   (Phases F/G). Runs attack scenarios against a feeder model and records both
   the *network* events and the *physical* grid response.
2. **`simulator/dataset/`** — the Task H dataset package. Combines the normal
   (Phase E) telemetry with the attack (Phase F/G) telemetry into one
   deterministic, validated, 25-column labelled dataset, plus ground truth and
   a provenance manifest.
3. **`results/dataset_final/`** — the generated datasets themselves, one set
   per feeder (`ieee37`, `ieee123`).

---

## 2. New package: `simulator/attack/` (Phases F & G — attack engine)

A reusable, feeder-agnostic attack engine. Attacks target **types** of assets
(e.g. "a voltage regulator"), not hard-coded feeder component IDs — the same
scenario code runs on both IEEE-37 and IEEE-123.

### Files

| File | Responsibility |
|---|---|
| `base.py` | `Attack`, `AttackContext`, `AttackActionResult`, `AttackError` — the contracts every scenario implements |
| `engine.py` | `AttackEngine` + `register_scenario`; the discovery surface for the dataset package |
| `scenarios/__init__.py` | `REGISTERED_SCENARIOS` and `scenario_id_for(attack_id, feeder_id)` |
| `scenarios/reconnaissance.py` | **Reconnaissance** — discovers devices/points (MITRE **T0846** Remote System Discovery) |
| `scenarios/unauthorized_command.py` | **Unauthorized Command** — sends an illegal control command (MITRE **T0855**) |
| `scenarios/parameter_modification.py` | **Parameter Modification** — alters a device parameter (MITRE **T0836** Modify Parameter) |
| `targets.py` | dynamic target selection against the feeder model |
| `mitre.py` | MITRE ATT&CK for ICS technique metadata (`TECHNIQUES` dict) |
| `events.py` | canonical attack event records |
| `physical.py` | the grid-side physical response of each scenario |
| `results.py` | `AttackResult` — ground-truth records, event rows, physical effect |
| `__main__.py` | CLI |
| `AttackScenarios.md` / `README.md` / `Workflow.md` | scenario + engine documentation |
| `test_attack_engine.py` | 36 unit/e2e tests |

### Registered scenarios

| attack_id | MITRE technique | kind |
|---|---|---|
| `reconnaissance` | `T0846` Remote System Discovery | network-only; minimal physical effect by design |
| `unauthorized_command` | `T0855` Unauthorized Command Message | network + grid response |
| `parameter_modification` | `T0836` Modify Parameter | network + grid response |

---

## 3. New package: `simulator/dataset/` (Task H — dataset generation)

### Files

| File | Responsibility |
|---|---|
| `schema.py` | canonical 25-column combined schema, `normal_row` / `attack_row`, sort key, strict `validate_combined` |
| `generator.py` | `generate_feeder_dataset`, `FeederDataset` — runs the F/G engine per scenario, collects normal + attack events |
| `exporter.py` | `export_feeder_artifacts`, `write_combined_csv`, `write_attack_only_csv`, `write_ground_truth_json` |
| `validator.py` | cross-checks every artifact (combined CSV, attack-only CSVs, ground truth) |
| `manifest.py` | `ManifestEntry`, `write_manifest` — provenance manifest per feeder with SHA-256 |
| `errors.py` | `DatasetError` hierarchy (generation / validation / export / manifest) |
| `__init__.py` | public API surface |
| `__main__.py` | CLI: `python3 -m simulator.dataset --feeders ieee37,ieee123 --seed 42` |
| `attack_dataset_workflow.md` | step-by-step workflow for the dataset pipeline |
| `test_dataset.py` | 10 tests against the schema/generator |

### Design guarantees (important for consumers)

- **Deterministic** — every run with the same seed produces byte-identical
  artifacts. Seed defaults to **42**.
- **Fail-closed** — a scenario that fails is recorded honestly in ground truth
  with its real failure reason; it is never fabricated. The CLI exits non-zero
  unless every requested feeder succeeded, combined, validated, and exported.
- **Validated** — the combined dataset must satisfy `validate_combined`
  (canonical columns, no duplicates, all expected scenario/attack ids present,
  consistent label/technique fields) before anything is written.
- **Traceable** — a per-feeder `manifest_<feeder>.json` records every artifact,
  its SHA-256, the seed, and the combined-file digest.

---

## 4. Generated artifacts (`results/dataset_final/`)

For each feeder `F` in `{ieee37, ieee123}`:

| Artifact | Description |
|---|---|
| `combined_<F>_dataset.csv` | the full 25-column labelled dataset (normal + attack rows) |
| `attack_<F>_<attack_id>_dataset.csv` | per-attack slices (3 per feeder) |
| `ground_truth_<F>.json` | scenario records: which attack, when, where, MITRE technique, physical effect |
| `manifest_<F>.json` | provenance: seed, artifact sizes, SHA-256 digests |

### Row counts (verified, deterministic)

| Feeder | combined | normal rows | attack rows |
|---|---|---|---|
| `ieee37` | 74,870 | 74,592 | 278 |
| `ieee123` | 157,221 | 156,576 | 645 |

Attack rows break down by scenario (same for both feeders): reconnaissance
dominates (274 on ieee37 / 641 on ieee123, consistent with a discovery-enabled
scenario); unauthorized command = 2 and parameter modification = 2 each.

### Determinism proof

Two fresh end-to-end runs produced **byte-identical** data artifacts. Combined
file SHA-256:

| Feeder | combined SHA-256 |
|---|---|
| `ieee37` | `46c060669b0c25ee7b5022651feea750ca09b4651ffb5d27d6c22f1cce173b51` |
| `ieee123` | `7da7d43d7a55cea91373124e51353438b1afe6871f6948e953af4058e81d8803` |

The only difference between runs is the `generated_at_utc` timestamp in the
manifests, exactly as intended.

The upstream **normal** datasets were hash-checked before and after and are
unchanged (e.g. ieee37 normal network events
`c547d868…c6b62`, ieee123 `59aa86ce…a4a3aa`).

---

## 5. Other files in this push

| File | What changed |
|---|---|
| `results/ieee37/normal_ieee37_network_events.csv` | now committed (Phase E normal network telemetry, attached to the dataset pipeline) |
| `results/ieee123/normal_ieee123_network_events.csv` | now committed (same) |
| `simulator/network/NetworkWorkflow.md` | updated network-events workflow documentation |
| `task_h_report.md` | full Task H technical report (21 points: requirements, design, schema, validation, determinism, results) |

---

## 6. How to reproduce

From the repo root (WSL `wsl -d Ubuntu` or any Python 3.11+ on POSIX paths):

```bash
# full test suite
python -m pytest -q

# regenerate both feeders' datasets from scratch
rm -rf results/dataset_final
python3 -m simulator.dataset --feeders ieee37,ieee123 --seed 42 \
    --events-dir results --results-dir results/dataset_rerun

# diff the regenerated combined CSVs against the committed ones (should be identical)
sha256sum results/dataset_rerun/combined_ieee37_dataset.csv results/dataset_final/combined_ieee37_dataset.csv
```

Requires Python `3.11+`, `opendssdirect`, and the Ausgrid-derived normal
profiles already in `grid_data/`.

---

## 7. What was NOT changed

- No changes to Phase A–E pipeline code (`grid_data/`, `simulator/power/`,
  `simulator/network/`) — the normal telemetry and network-event sources are
  byte-unchanged.
- No hard-coded per-feeder attack targeting; the attack engine remains
  feeder-agnostic.
- No fabricated failures — every failed scenario is recorded honestly.

---

## 8. Suggested next steps (for discussion)

- Extend `REGISTERED_SCENARIOS` with false-measurement / communication-disruption
  scenarios (they were scoped out of Task H but the `Attack` contract is ready).
- Add a third feeder (`ieee69`-style) to prove cross-feeder generalisation.
- Feed `ground_truth_<F>.json` into a downstream intrusion-detection experiment.