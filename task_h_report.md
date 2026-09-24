# Task H Report -- Combined, Deterministic, Attack-Labeled Dataset

## 1. Scope and objective

Task H produces, per feeder (ieee37, ieee123), a combined 25-column dataset
that merges the Phase E normal network events (16 columns, verbatim) with the
Phase F/G attack-engine events (6 attack columns) and 3 Task H annotation
columns (``label``, ``attack_id``, ``mitre_technique``).  The pipeline is
deterministic (seed 42) and fail-closed: nothing is fabricated, shuffled,
renumbered or silently dropped.

## 2. Files created (``simulator/dataset``)

- ``__init__.py`` -- package exports
- ``errors.py`` -- canonical fail-closed exception hierarchy (DatasetError,
  DatasetGenerationError, DatasetValidationError, DatasetExportError,
  DatasetManifestError)
- ``schema.py`` -- canonical 25-column schema + row builders + deterministic
  sort key + strict validation
- ``generator.py`` -- per-feeder deterministic generate_feeder_dataset()
- ``exporter.py`` -- CSV/JSON/manifest writers
- ``validator.py`` -- strict row/combined/ground-truth validators
- ``manifest.py`` -- determinism/integrity ledger writer
- ``__main__.py`` -- CLI ``python3 -m simulator.dataset``
- ``test_dataset.py`` -- 10 Task H unit tests
- ``attack_dataset_workflow.md`` -- workflow documentation

## 3. CLI usage

```bash
python3 -m simulator.dataset --feeders ieee37,ieee123 \
  --seed 42 --events-dir results --results-dir results/dataset_final
```

Exit code 0 only when every feeder generates, combines, validates and exports.

## 4. Generated artifacts (results/dataset_final)

```
combined_ieee37_dataset.csv                  14,554,467 B
combined_ieee123_dataset.csv                 31,128,824 B
attack_ieee37_{rec/unauth/param}_dataset.csv   64,953 B each
attack_ieee123_{rec/unauth/param}_dataset.csv 152,560 B each
ground_truth_ieee37.json                       481,023 B
ground_truth_ieee123.json                    1,097,704 B
manifest_ieee37.json / manifest_ieee123.json (integrity ledgers)
```

## 5. Per-feeder statistics (seed 42)

| Feeder  | Normal rows | Attack rows | Combined | Scenarios OK | Failed |
|---------|------------:|------------:|---------:|-------------:|-------:|
| ieee37  |      74,592 |         278 |   74,870 |            3 |      0 |
| ieee123 |     156,576 |         645 |  157,221 |            3 |      0 |

Attack breakdown (matches Phase G expectations exactly):

- ieee37: reconnaissance 274, unauthorized_command 2, parameter_modification 2
- ieee123: reconnaissance 641, unauthorized_command 2, parameter_modification 2
- MITRE: T0846 (recon), T0855 (unauthorized command), T0836 (parameter mod)

## 6. Combined CSV schema (25 columns)

Phase E verbatim ``NETWORK_EVENT_COLUMNS`` (16) + Phase F/G
``ATTACK_EVENT_COLUMNS[16:]`` (6: injected, replay_of_event_id, original_value,
reported_value, command_id, result) + Task H (``label``, ``attack_id``,
``mitre_technique``).  NORMAL rows carry no attack metadata; ATTACK rows carry
attack_id + MITRE technique + scenario_id.

## 7. Determinism proof

Two full runs (fresh output dirs, seed 42) produce byte-identical sha256 for
every data artifact:

- combined_ieee37 sha256 = 46c060669b0c25ee7b5022651feea750ca09b4651ffb5d27d6c22f1cce173b51 (both runs)
- combined_ieee123 sha256 = 7da7d43d7a55cea91373124e51353438b1afe6871f6948e953af4058e81d8803 (both runs)
- all attack-only CSVs and ground-truth JSONs identical across runs
- the manifest differs only in the ``generated_at_utc`` wall-clock field
  (provenance, by design); its ``determinism_digest`` (combined sha256) matches

## 8. Ground truth (ground_truth_<feeder>.json)

One JSON per feeder: feeder_id, seed, scenario_id, normal block (verbatim),
attacks block (verbatim AttackResult.ground_truth() records + event rows +
physical_effect + as_dict), and failed_scenarios.  Successfully verified
against the F/G engine output.

## 9. Source immutability

The Phase E normal CSVs are sha256-fingerprinted and verified unchanged after
both runs:

- results/ieee37/normal_ieee37_network_events.csv
   c547d868923199d1379da4df9d53b43bf3ba3a60a9ae1452ced8813fdc3c6b62
- results/ieee123/normal_ieee123_network_events.csv
   59aa86cec48479a5f408ec62263921e08c70530145562524ed6fac6a47a4a3aa

## 10. Regression

- ``python3 -m unittest simulator.network.test_network_events -v`` -> 17 OK
- ``python3 -m unittest simulator.attack.test_attack_engine -v`` -> 36 OK
- ``python3 -m unittest simulator.dataset.test_dataset -v`` -> 10 OK
- ``python3 -m pytest -q`` -> 84 passed, 2 skipped, 24 subtests passed

## 11. Fail-closed behavior (verified)

Missing normal CSV raises DatasetGenerationError and the CLI reports FAIL with
exit 1.  Validation violations raise DatasetValidationError.  Scenario failures
are recorded (never fabricated) in ground truth / manifest.  Empty/out-of-order
rows are rejected by schema.py's validate_row / validate_combined.

## 12. DNP3 and realism limits

``protocol=DNP3`` is a logical label only.  No sockets, packets or real DNP3
traffic exist anywhere in the pipeline.  Values/effects come from the Phase F/G
simulation engine or Phase E data; nothing is invented at Task H time.

## 13. Scope boundaries

Implemented: Task H combined dataset, annotations, manifest, determinism,
validation, CLI, tests.  Not implemented (as explicitly deferred): G4/G5/G6.

## 14. Future scenarios

New scenarios register via ``simulator.attack.scenarios.REGISTERED_SCENARIOS``
and flow through the pipeline automatically; Task H contains no
attack-id-specific logic.

## 15. Environment

WSL Ubuntu Python 3.14.4, opendssdirect.py 0.9.4, pytest 9.0.2.  Verify with the
test commands in section 10.