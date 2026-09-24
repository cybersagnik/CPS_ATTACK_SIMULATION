# Task H -- Combined Attack Dataset (Attack_Dataset_Workflow)

Task H consumes the Phase E normal network-events CSVs and the Phase F/G attack
engine, and produces a **combined, deterministic, attack-labeled dataset** per
feeder (25 columns), plus per-scenario attack-only CSVs, a ground-truth JSON,
and an integrity (manifest) JSON -- all fail-closed.

## Scope

Inputs (read-only, never modified):

- ``results/<feeder>/normal_<feeder>_network_events.csv`` (Phase E)
- ``simulator.attack`` engine: ``simulator.attack.engine.AttackEngine``,
  ``simulator.attack.scenarios.REGISTERED_SCENARIOS``,
  ``simulator.attack.results.AttackResult`` (``ground_truth()``,
  ``event_rows()``, ``as_dict()``)

Outputs for each feeder (written to ``--results-dir``):

- ``combined_<feeder>_dataset.csv`` -- 25-column combined dataset
- ``attack_<feeder>_<attack_id>_dataset.csv`` -- per-scenario attack-only CSV
- ``ground_truth_<feeder>.json`` -- ground truth (normal + attacks, verbatim)
- ``manifest_<feeder>.json`` -- integrity ledger with per-artifact sha256

## Schema (25 columns)

``DATASET_COLUMNS`` = Phase E ``NETWORK_EVENT_COLUMNS`` (16) + Phase F/G
``ATTACK_EVENT_COLUMNS[16:]`` (6: injected, replay_of_event_id,
original_value, reported_value, command_id, result) + Task H
(``label``, ``attack_id``, ``mitre_technique``).  Sectioned and documented in
``simulator/dataset/schema.py``.

Row labels: ``NORMAL`` (Phase E verbatim, no attack metadata) and ``ATTACK``
(Phase F/G event + attack_id + MITRE technique).  MITRE ids come from the
scenario's registered mapping (T0846 reconnaissance, T0855 unauthorized
command, T0836 parameter modification).

## Determinism

- Default seed 42; the generator runs each registered scenario exactly once
  through the canonical engine with an explicit ``AttackContext``
  (``event_path`` = normal CSV, reference timestamp, seed).
- Combined rows are ordered by the canonical ``combined_sort_key`` =
  ``(timestamp ISO-8601, lane NORMAL<ATTACK, sequence, event_id)`` -- never
  shuffled, never renumbered.
- Verified by double run: all data artifacts are byte-identical (same sha256);
  only the manifest's ``generated_at_utc`` wall-clock field differs (by design).

## Fail-closed guarantees

- Missing Phase E CSV, an engine scenario that cannot reach SUCCESS, or any
  validation violation **raises** -- the pipeline exits non-zero and records
  every failure.  Failed scenarios are listed verbatim in the ground truth /
  manifest (with their real failure reason); nothing is ever fabricated or
  silently dropped.
- Source Phase E CSVs are sha256-fingerprinted in the manifest before/after
  verification (their bytes never change).

## Usage

```bash
python3 -m simulator.dataset \
  --feeders ieee37,ieee123 \
  --seed 42 \
  --events-dir results \
  --results-dir results/dataset_final
```

Exit code 0 only when every requested feeder generates, combines, validates and
exports successfully.

## Tests

```bash
python3 -m unittest simulator.dataset.test_dataset -v
python3 -m unittest simulator.network.test_network_events -v
python3 -m unittest simulator.attack.test_attack_engine -v
python3 -m pytest -q
```

Full suite: 84 passed, 2 skipped.