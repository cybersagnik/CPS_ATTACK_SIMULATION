# Attack Engine Workflow (simulator/attack) — Task F / G

Canonical workflow record for the attack engine (Task F) and the first three
MITRE ATT&CK for ICS scenarios (Task G) of the Phase 1 attack-dataset pipeline.
Companion document to `simulator/network/NetworkWorkflow.md` (the Phase E
normal-network baseline this phase builds on top of) — see also the package
quick-guide in `simulator/attack/README.md`.

Pipeline position:

    Feeder Model + Grid Data → Common Grid Model → Normal Simulation
        → Network / Event Layer (Phase E, simulator/network)
        → ⭐ Attack Engine + MITRE Scenarios (this package)  →  Attack Dataset (Phase H)

## 1. Purpose

Insert a generic, feeder-agnostic attack layer between the Phase E normal
network events and the future attack-dataset exporter (Task H).  The layer:

- discovers targets dynamically from the *actual* data it is given — the
  normal network events (reported measurement points) and the common grid
  model (controllable assets and their parameters);
- executes attack scenarios expressed through a single `Attack` abstraction;
- reports every execution as one structured `AttackResult` (ground truth +
  attack network events) that Task H can consume verbatim.

## 2. Scope / non-goals (hard rules)

- No real DNP3/Modbus/IEC packets, no sockets, no real SCADA traffic.
  `protocol="DNP3"` remains a logical label on every event (Phase E rule).
- The physical telemetry and the normal-scenario code are never modified.
- No hardcoded feeder id, bus number, component id or point id in production
  code — selection runs on generic kinds and point-id patterns only.
- No compatible target → structured FAILED result; never invent/silently
  attack something.
- Task H (dataset export) is intentionally **not** implemented here; this
  package only exposes the consumption surface (`ground_truth()`,
  `event_rows()`, `SCENARIOS`).

## 3. Inputs / outputs

Reads (all optional — the engine works with whatever is present):

- `results/<feeder>/normal_<feeder>_network_events.csv` — measurement-point
  inventory (what the SCADA layer actually reported).
- A loaded `FeederModel` (`simulator.power.loader.FeederLoader`) — control
  surface (controllable assets / parameters) and the base loads OpenDSS needs
  for the physical-effect hook.
- A reference `timestamp` (attack events are stamped with it).

Writes (in-memory only):

- One `AttackResult` per execution: flat ground truth + `ATTACK_EVENT_COLUMNS`
  rows.  Nothing is persisted by this package.

## 4. Implementation workflow (what was done)

1. **Declarations / catalog** — `mitre.py`: verified MITRE ATT&CK for ICS
   technique catalog `T0846` (Remote System Discovery / Discovery),
   `T0855` (Unauthorized Command Message / Impair Process Control),
   `T0836` (Modify Parameter / Impair Process Control), each with a recorded
   rationale.  Technique ids checked against the official
   `https://attack.mitre.org` source; nothing invented.
2. **Event model** — `events.py`: `AttackEvent` = Phase E 16-column schema
   plus the planned extension (`injected`, `replay_of_event_id`,
   `original_value`, `reported_value`, `command_id`, `result`); new logical
   message types `QUERY` / `COMMAND` / `REPORT`; strict validation.
3. **Discovery & selection** — `targets.py`: `Inventory` of measurement
   points (from event rows/CSV) and control points (from the model's
   `controllable_parameters()`); `TargetSelector` picks deterministically
   (`sorted candidates`, `index = seed % len`) and supports a *cascade*
   `prefer=` tier (switch → capacitor → load) instead of a degraded sort
   hint; `validate_target` enforces existence / feeder / compatibility before
   execution.
4. **Abstraction** — `base.py`: `Attack` (`select_targets`,
   `validate_preconditions`, `execute`, `generate_events`, `metadata`),
   `AttackContext` (explicit dependencies; inventory optionally pre-built),
   `AttackActionResult`.
5. **Orchestration** — `engine.py`: `AttackEngine.run()` wires the four
   steps, builds the inventory when absent, validates the selected target,
   assigns deterministic `event_id = <scenario_id>-atk<NNN>` identities and
   always returns a structured `AttackResult` — FAILED with a reason at the
   first unsound stage, never a silent random attack.
6. **Ground truth** — `results.py`: `AttackResult.ground_truth()` (flat
   record incl. MITRE ids, target, original/injected/reported values) and
   `event_rows()` (event rows in the attack schema order) — the Task H
   surface.
7. **Physical effect** — `physical.py`: builds a fresh OpenDSS circuit per
   solve via the existing `simulator.power.dss_builder`, solves baseline vs.
   overridden, returns voltage/power deltas.  Limitation documented honestly:
   regulator `tap_position` is logically commandable but not physically
   computable (Phase D holds taps at nominal; no r-bus) → `supported=False`
   instead of a fabricated number.  A bug was fixed here: `DssBuilder` never
   emits normally-open switch elements, so physical.py now creates them
   (disabled) and a "close the open switch" attack produces a genuine effect.
8. **Scenarios** — `scenarios/`: `ReconnaissanceAttack` (T0846),
   `UnauthorizedCommandAttack` (T0855), `ParameterModificationAttack` (T0836);
   registered in `REGISTERED_SCENARIOS` / `engine.SCENARIOS`; scenario ids
   follow the Phase E convention `normal_<feeder>`-style `<attack_id>_<feeder_id>`.
9. **Tests** — `test_attack_engine.py` (36 tests): catalog, point/inventory/
   selection units, event model, engine wiring, fail-closed behaviour,
   **future-feeder independence** (a never-seen feeder id works), real-data
   execution on ieee37+ieee123, and OpenDSS-gated physical-effect assertions.
10. **Validation & audit** — real-data runs for both feeders; hardcoding
    audit (feeder/bus ids only in tests/docstrings; production uses generic
    `startswith("BUS_")`-style pattern classification); Phase E tests kept
    green throughout.

## 5. Decisions & conventions worth knowing

- `injected` semantics (canonical in `events.py`): `1` = message did not
  originate from the Phase E normal dialog (forged COMMAND/QUERY, or a REPORT
  carrying an untrustable — tampered — value); `0` = genuine device echo of a
  true post-action state (e.g. an honest ack of an unauthorized command).
- Scenario capability follows the feeder: ieee37 has no switches/capacitors
  → Unauthorized Command disables a load; ieee123 has switches → it opens/
  closes a switch.  All from the same code path.
- Parameter Modification scales kw *and* kvar so the load power factor is
  preserved (honest effect), reports the tampered value as the operating value
  (`injected=1` on the REPORT), and keeps the original value everywhere.
- A fresh physical circuit per solve → state restoration is automatic; nothing
  leaks between runs.
- Real-data counts (unique telemetry points): ieee37 = 221, ieee123 = 465.

## 6. Validation runs (the command list)

See `simulator/attack/AttackScenarios.md` → "Command list" for the full, ready
to copy-paste sequence (install, tests, scenario CLI, pytest, audit).

## 7. Files

- `simulator/attack/__init__.py`
- `simulator/attack/__main__.py` — CLI (`python3 -m simulator.attack`)
- `simulator/attack/base.py` — `Attack`, `AttackContext`, `AttackActionResult`
- `simulator/attack/engine.py` — `AttackEngine`, `SCENARIOS`
- `simulator/attack/targets.py` — inventory / selection / validation
- `simulator/attack/results.py` — `AttackResult` + Task H surface
- `simulator/attack/events.py` — `AttackEvent` + validation
- `simulator/attack/physical.py` — OpenDSS effect hook
- `simulator/attack/mitre.py` — verified technique catalog
- `simulator/attack/scenarios/` — the three scenarios + registry
- `simulator/attack/test_attack_engine.py` — 36 tests
- `simulator/attack/README.md`, `simulator/attack/Workflow.md`,
  `simulator/attack/AttackScenarios.md` — documentation
- `simulator/network/NetworkWorkflow.md` — Phase E baseline (+ Phase F/G section)