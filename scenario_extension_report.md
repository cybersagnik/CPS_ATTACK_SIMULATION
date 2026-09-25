# Scenario Extension Report — False Measurement, Communication Disruption, Multi-Step Attack

Implementation report for adding the three remaining MITRE ATT&CK for ICS
attack scenarios to the existing F/G attack engine.  Companion to
`task_h_report.md` (Task H dataset) and `UPDATES.md`.  No Task H / dataset
package code was touched; the existing three scenarios are unchanged and keep
passing.

## 1. Overview

The attack engine (Task F) shipped with three scenarios (Task G): Reconnaissance
(T0846), Unauthorized Command (T0855), Parameter Modification (T0836).  These
three extension scenarios complete the set described in the project README
(Scenario 5 False Measurement, Scenario 6 Communication Disruption, Scenario 7
Multi-Step Attack).  All six are proper `Attack` implementations that run
through the *existing* `AttackEngine` — the engine, event model, result
contract, and selection machinery were **not** redesigned or modified.

New scenarios added to the `REGISTERED_SCENARIOS` registry automatically appear
in the CLI `--attack` choices.  The `simulator/dataset` generator also iterates
that registry, so Task H picks them up without any change to its code.

## 2. Files changed

Added (production, `simulator/attack/scenarios/`):

| file | scenario |
|------|----------|
| `false_measurement.py` | `FalseMeasurementAttack` (T0856) |
| `communication_disruption.py` | `CommunicationDisruptionAttack` (T0804) |
| `multi_step_attack.py` | `MultiStepAttack` (T0846 + T0855 + T0836) |

Modified:

| file | change |
|------|--------|
| `simulator/attack/mitre.py` | added verified mappings T0856, T0803, T0804 + tactic constants `TACTIC_EVASION`, `TACTIC_INHIBIT_RESPONSE_FUNCTION`; catalog now six techniques |
| `simulator/attack/scenarios/__init__.py` | registered the three new scenarios, extended `__all__` |
| `simulator/attack/test_attack_engine.py` | +14 tests (36 → 50), scenario table loop now covers all six |

Docs: `simulator/attack/AttackScenarios.md` (rewritten for six scenarios),
`simulator/attack/README.md`, `simulator/attack/Workflow.md`,
`simulator/network/NetworkWorkflow.md`, `simulator/dataset/attack_dataset_workflow.md`.
Source datasets (`results/*/normal_*_network_events.csv`) were **not** modified.

## 3. Scenario 4 — False Measurement (T0856 Spoof Reporting Message)

The adversary forges the RTU → SCADA reporting message of a real measurement
point so the operator observes `reported = actual × measurement_scale`
(config `measurement_scale`, default **1.05**).  The grid itself is never
changed — the physical solve uses an empty override set, so the deltas are the
honest **zero**.

Event footprint (2 events, `injected=1` both): attacker-forged `QUERY`
(READ dialog, `result=READ`) + forged `REPORT` carrying the physical truth as
`original_value` and the spoofed value as `reported_value` (`result=SPOOFED`).
Selection is dynamic: numeric measurement point, prefer voltage (else current,
else power).

Real-data result (seed 42):

| feeder | target point | physical truth | reported to operator | physical deltas |
|--------|--------------|----------------|----------------------|-----------------|
| ieee37 | `BUS_718_VPU_AB` (voltage) | 0.8966 pu | 0.9415 pu | 0.0 / 0.0 / 0.0 |
| ieee123 | `BUS_21_VPU_AB` (voltage) | 0.9028 pu | 0.9479 pu | 0.0 / 0.0 / 0.0 |

## 4. Scenario 5 — Communication Disruption (T0804 Block Reporting Message)

The adversary blocks the genuine reporting message of a measurement point:
the measurement is taken truthfully but `delivery_status=DROPPED`, so SCADA
never receives it while the grid keeps operating (message loss, no physical
change — zero deltas).

Event footprint (2 events): injected `QUERY` with `result=BLOCK` (the
instruction to a compromised RTU to withhold the point) + genuine `REPORT`
(`injected=0`, real value) marked `delivery_status=DROPPED`, `result=BLOCKED`.
This is the scenario the `events.py` contract point to: a non-normal delivery
status is allowed exactly where a scenario requires it.

Real-data result (seed 42): both feeders block `BUS_718_VPU_AB` / `BUS_21_VPU_AB`,
true value preserved on the row, grid unchanged (deltas 0.0).

## 5. Scenario 6 — Multi-Step Attack (T0846 + T0855 + T0836)

The adversary chains three techniques into one chronological *timeline* (the
README Scenario 7 shape): reconnaissance → unauthorized command → parameter
modification.  The multi-step scenario composes the three registered
single-step scenarios, replaying each through the same `AttackEngine` with
stage timestamps stepped +1s (`t`, `t+1s`, `t+2s`) so the exported event
timeline is strictly chronological under the engine's deterministic sort.

Design points:

- **One scenario_id** for the whole timeline; the engine re-numbers all events
  contiguously (`<scenario_id>-atk001…`), so a single numbered sequence (not
  isolated malicious rows).
- **Composite preconditions** are verified *before* any stage executes — the
  engine fails cleanly if, e.g., the feeder has no physically supported load
  coefficient or commandable asset.
- **Combined physical effect** solves the real feeder at baseline and once with
  the *union* of every modifying stage's overrides (the honest end-state
  footprint).  Per-stage deltas are preserved in `metadata["stages"]`.
- MITRE mapping is the union `T0846;T0855;T0836`; the Task H `mitre_technique`
  column uses the lead id `T0846` (first of the union).

Real-data result (seed 42):

| feeder | events | stages | combined footprint (`source_p_kw`) | overrides |
|--------|--------|--------|-----------------------------------|-----------|
| ieee37 | 278 | recon(274) + command(2) + param(2) | −42.79 kW | `load/018` (disabled) + `load/022` (kw,kvar ×1.35) |
| ieee123 | 645 | recon(641) + command(2) + param(2) | +15.08 kW | `switch/010` (opened) + `load/022` (kw,kvar ×1.35) |

## 6. MITRE mapping verification (authoritative sources)

Mappings were verified against ATT&CK for ICS v15.1
(`https://attack.mitre.org/versions/v15/techniques/…`) and the corresponding
CISA/evasion-strategy entries — nothing was guessed:

| technique | name | tactic | used by |
|-----------|------|--------|---------|
| T0846 | Remote System Discovery | Discovery (TA0102) | (existing) |
| T0855 | Unauthorized Command Message | Impair Process Control (TA0106) | (existing) |
| T0836 | Modify Parameter | Impair Process Control (TA0106) | (existing) |
| **T0856** | Spoof Reporting Message | Evasion (TA0103); also Impair Process Control (TA0106) | False Measurement |
| **T0804** | Block Reporting Message | Inhibit Response Function (TA0107) | Communication Disruption |
| **T0803** | Block Command Message | Inhibit Response Function (TA0107) | catalog (sibling; not a shipped scenario) |

Note: T0865 in the current matrix is *Spearphishing Attachment* (Initial
Access) and is **not** a valid mapping for false measurements — T0856 is the
correct one.  Every mapping carries a recorded rationale in `mitre.py`.

## 7. Physical vs cyber truth separation

The three extension scenarios were built on the strict separation already
required by the engine:

- **Physical truth** = what the real OpenDSS solve produces (the `physical_effect`
  snapshot) — never fabricated.
- **Cyber/network truth** = the event rows (`QUERY/COMMAND/REPORT`, `injected`,
  `original_value`, `reported_value`, `delivery_status`, `result`).
- **Reported truth** = what the operator sees (`reported_value`,
  `delivery_status`).

False Measurement: cyber/reported truth differ from physical truth (deltas zero
by honest computation).  Communication Disruption: cyber truth says “dropped”,
physical truth says “unchanged”.  Multi-Step: cyber truth is a staged timeline and
the physical truth is the combined end-state footprint.

## 8. Contract compliance

`AttackResult` (`result`, `status`, `target`, `selection`, `original/injected/
reported_value`, `command_id`, `mitre`, `events`, `physical_effect`, `metadata`,
`ground_truth()`, `event_rows()`, `as_dict()`) is untouched.  `SCENARIOS` /
`REGISTERED_SCENARIOS` registry semantics are unchanged (it now simply holds six
entries).  Every event passes `validate_attack_event`; event ids /
`ATTACK_EVENT_COLUMNS` ordering preserved.  Nothing is persisted by this
package (export still belongs to Task H).

## 9. Tests added

In `simulator/attack/test_attack_engine.py` (36 → **50** tests):

- catalog: T0856 / T0803 / T0804 verified names & tactics; six-id catalog loop;
- `test_mitre_mappings_of_scenario_implementations` — asserts the registry is
  exactly the six attacks and each maps to its technique(s);
- `test_all_registered_scenarios_execute_on_both_real_feeder_models` (renamed)
  — every registered scenario on ieee37 + ieee123 must SUCCESS and produce a
  computed physical effect (except reconnaissance);
- false measurement: forged REPORT fields, reported≠original, honest zero
  deltas, on both feeders;
- communication disruption: `delivery_status=DROPPED`, genuine measurement,
  grid kept operating, zero deltas;
- multi-step: stage composition order, strictly chronological stage timestamps,
  message-type counts, union ground-truth mitre ids, combined footprint with
  per-stage deltas.

## 10. Run results

CLI (both feeders, all six scenarios): **all `result: SUCCESS`, exit code 0**
(`python3 -m simulator.attack --feeder ieee37` / `--feeder ieee123`).  Per
scenario event counts on ieee37 / ieee123:

| scenario | ieee37 | ieee123 |
|----------|--------|---------|
| reconnaissance | 274 | 641 |
| unauthorized_command | 2 | 2 |
| parameter_modification | 2 | 2 |
| false_measurement | 2 | 2 |
| communication_disruption | 2 | 2 |
| multi_step_attack | 278 | 645 |

Determinism: double-run with seed 42 produces byte-identical `as_dict()`
results for every new scenario.

## 11. Regression and source integrity

- Full suite before: `84 passed, 2 skipped, 24 subtests`.  After: **`98 passed,
  2 skipped, 60 subtests`** (Windows, Python 3.14.7, opendssdirect 0.9.4).
- All Phase E network tests and all Task H dataset tests remain green (no
  dataset code touched).
- `simulator/attack` production-code audit for hardcoded feeder/bus/point ids:
  **zero executable-logic hits** (only docstring examples, as before).
- Source input hashes unchanged:
  - `results/ieee37/normal_ieee37_network_events.csv`
    = `c547d868923199d1379da4df9d53b43bf3ba3a60a9ae1452ced8813fdc3c6b62`
  - `results/ieee123/normal_ieee123_network_events.csv`
    = `59aa86cec48479a5f408ec62263921e08c70530145562524ed6fac6a47a4a3aa`
- No modification to `simulator/dataset/*` (Task H) code or config.

## 12. Limitations and notes

- False Measurement and Communication Disruption model the *reporting path*
  via the logical event layer (`protocol="DNP3"` is a label; no sockets, no
  packets).  The “blocked” message is represented as a row with
  `delivery_status=DROPPED`, not as an altered transport.
- The multi-step scenario reuses the composed single-step scenarios; it does
  not add a fourth “observe then modify again” stage.  The “observe” step is
  captured as the combined physical footprint plus per-stage deltas.
- Physical effects require `opendssdirect`; when absent those solves report
  `status=unavailable` with a reason (never fabricated numbers) and the
  OpenDSS-gated tests skip.
- `measurement_scale` (default 1.05) and `modify_delta` (existing, 0.35) are
  config keys; the dataset generator passes `config={}`, so defaults apply.
- Scenario ids follow the Phase E convention `<attack_id>_<feeder_id>`; all six
  ids are feeder-scoped, deterministic, and registrable by Task H as-is.