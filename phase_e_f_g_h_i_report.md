# Validation Report -- E -> F -> G -> H -> I Pipeline (PASS)

Scope: independent end-to-end validation of the pipelined teammate work
(no redesign, no test-forcing).  Every phase below was executed from a clean
state with the canonical commands and the real data on disk.  Commits
present: `1623dce` (Task H + F/G), `1fbc967` (scenarios + command list),
`a74e459` (JSON-safe export + manifest + tests), `c155b1b` (Task I ground truth).

## Summary

| Phase | Verdict | Evidence |
|-------|---------|----------|
| E    Normal network events  | PASS | regenerated both CSVs; events = POLL+TELEM per timestamp, values copied verbatim from grid telemetry |
| F/G  Attack engine/scenarios | PASS | `python3 -m simulator.attack --feeder {ieee37,ieee123}` -> all 6 MITRE-ICS scenarios SUCCESS, physical deltas real |
| H    Combined dataset       | PASS | `python3 -m simulator.dataset` -> combined CSV byte-identical to committed, 6/6 scenarios OK, 0 failed |
| I    Ground-truth validation | PASS | `--validate-ground-truth` -> PASS both feeders, all 12 scenario reports + all checks PASS |

## Hardcoding audit

Grep for `ieee37|ieee123`, `load/<id>`, and `bus <3-digit>` across the
production pipeline modules (`simulator/network/*`, `simulator/attack/*`,
`simulator/dataset/*`, non-test):

* **No feeder-specific logic.**  Matches are confined to docstrings/examples,
  CLI `--help` text, and test files.
* Target selection is fully generic (kind/sub-kind/concept/parameter +
  deterministic seed), no point id, bus number or component id is hardcoded.
  Confirmed by the `FutureFeederIndependenceTests` (engine runs an unseen
  `future_feeder`) and by `test_no_hardcoded_attacks_in_model`.

## Phase E -- normal network events (PASS)

Commands (canonical):
```
python3 -m simulator.network.normal --feeder ieee37  --results-dir results/ieee37
python3 -m simulator.network.normal --feeder ieee123 --results-dir results/ieee123
```

Results (values identical to committed artifacts):

* `ieee37` : 336 timestamps, 74,592 events = 336 POLL + 74,256
  TELEMETRY (36,288 bus + 36,288 current + 1,680 summary).
* `ieee123`: 336 timestamps, 156,576 events = 336 POLL + 156,240
  TELEMETRY (67,536 bus + 87,024 current + 1,680 summary).

Determinism: regenerated files are byte-identical to the committed CSVs after
CRLF normalization (the Python `csv` writer emits `\r\n`; the committed blobs
carry LF -- a line-ending artifact of git/OS checkout, not content drift).
Telemetry -> event mapping is 1:1 and verbatim (e.g. bus 718 `vpu_AB` =
`0.8966879538876935` appears unchanged in the normal TELEMETRY row).

## Phase F/G -- attack engine + MITRE-ICS scenarios (PASS)

```
python3 -m simulator.attack --feeder ieee37
python3 -m simulator.attack --feeder ieee123
```

All 6 registered scenarios reach SUCCESS on both feeders
(`reconnaissance`, `unauthorized_command`, `parameter_modification`,
`false_measurement`, `communication_disruption`, `multi_step_attack`).

Representative evidence (deterministic, seed 42):

| scenario | ieee37 target | ieee123 target | expected deltas |
|----------|---------------|----------------|-----------------|
| reconnaissance / T0846 | RTU (scope) | RTU (scope) | n/a (discovery) |
| unauthorized_command / T0855 | `CTRL_load_load_018_enabled` (load) | `CTRL_switch_switch_010_closed` (switch) | dP_kW = -65.79 / +4.06 (non-zero) |
| parameter_modification / T0836 | `CTRL_load_load_022_kvar_multiplier` | same | dP_kW = +22.32 / +11.14 (non-zero) |
| false_measurement / T0856 | `BUS_718_VPU_AB` (voltage) | `BUS_21_VPU_AB` | 0.0 (forgery, grid untouched -- honest zero) |
| communication_disruption / T0804 | `BUS_718_VPU_AB` (voltage) | `BUS_21_VPU_AB` | 0.0 (blocked reporting, grid operates) |
| multi_step_attack / T0846;T0855;T0836 | RTU scope, 278 events | RTU scope, 645 events | combined non-zero footprint |

Target preference is behaviourally correct: `unauthorized_command` prefers
switch > capacitor > load, so ieee123 (modelled switches) picks a switch while
ieee37 (no switches in model) falls back to a load -- both via generic
selection, matching the engine tests.

## Phase H -- combined 25-column dataset (PASS)

```
python3 -m simulator.dataset --feeders ieee37,ieee123 \
  --events-dir results --results-dir results/dataset
```

```
OK   ieee37
OK   ieee123
  ieee37:  combined=75152 normal=74592 attack=560   scenarios_ok=6 scenarios_failed=0
  ieee123: combined=157870 normal=156576 attack=1294 scenarios_ok=6 scenarios_failed=0
Task H: 2 feeder(s) OK, deterministic (seed=42).
```

Determinism: combined CSVs and per-scenario attack CSVs are **byte-identical**
(CRLF-normalized) to the committed artifacts; row ordering follows the
canonical `timestamp -> lane(NORMAL<ATTACK) -> sequence -> event_id` key; 25
columns are exactly `16 Phase E + 6 Phase F/G + 3 Task H`, imported verbatim.

Ground-truth JSONs differ from the committed copies only in last-ULP digits
of the OpenDSS `physical_effect` baseline/affected floats (~1e-13 relative,
e.g. `2325.4434326820265` vs `2325.443432682025`) -- a solver-output
characteristic stored *only* in the ground-truth metadata, never in the
combined rows.  All structural fields (targets, event ids, sequences, MITRE,
labels, timestamps, deltas) are identical.

## Phase I -- ground-truth validation (PASS)

```
python3 -m simulator.dataset --feeders ieee37,ieee123 \
  --events-dir results --results-dir results/dataset --validate-ground-truth
```

```
PASS ieee37:  total=75152 normal=74592 attack=560
    communication_disruption: T0804  PASS    false_measurement: T0856 PASS
    multi_step_attack:        T0846;T0855;T0836 PASS (window t..t+2s)
    parameter_modification:   T0836  PASS    reconnaissance:      T0846 PASS
    unauthorized_command:     T0855  PASS
PASS ieee123: total=157870 normal=156576 attack=1294
    (all 6 scenarios PASS, same MITRE labels)
Ground-truth validation: PASS
```

Every check family (`ground_truth_present, counts, labels, mitre, scenario_id,
attack_id, cross_consistency, window, metadata, ground_truth_flag,
multi_step_chronology`) is PASS; zero errors.

## End-to-end row trace (one row, t = 2010-07-01T00:00:00)

`false_measurement_ieee37-atk002` (REPORT, ATTACK label):

1. **Grid truth** `results/ieee37/normal_ieee37_bus_telemetry.csv`:
   bus 718 `vpu_AB = 0.8966879538876935`.
2. **Normal event** `normal_ieee37-ev00000044` TELEMETRY
   `BUS_718_VPU_AB = 0.8966879538876935` (verbatim, Phase E).
3. **Attack** REPORT `BUS_718_VPU_AB = 0.941522 = 0.8966879538876935 * 1.05`
   (spoof scale), `injected=1`, `result=SPOOFED`, `original_value` = physical
   truth, `reported_value` = spoofed value.
4. **Ground truth** `cyber_matches_physical=false`, `mitre=T0856`,
   `label=ATTACK`, `attack_id=false_measurement`.

Timestamp -> grid state -> network event -> attack -> MITRE -> label is fully
traceable in the committed artifacts.

## Tests

```
python3 -m pytest -q                                  -> 71 passed (tests/)
python3 -m pytest -q simulator/network simulator/attack \
              simulator/dataset simulator/power        -> 125 passed, 2 skipped, 72 subtests passed
```

The 2 skipped are the `RUN_E2E=1`-gated Phase D end-to-end tests
(`test_e2e_scenario.py`), validated separately previously.  No test was
modified, skipped to green, or added for validation purposes.

## Findings

* **PASS** on all four phases; no defect found that required a code fix.
* Non-issues recorded for completeness:
  * Line endings (`csv` writer -> `\r\n`; git blobs LF) -- content-identical.
  * OpenDSS `physical_effect` floats in ground-truth metadata vary at ~1e-13
    between runs/platforms -- solver ULP behaviour; not in the dataset rows.