# simulator/attack — Attack Engine & MITRE-ICS Scenarios (Task F / G)

Feeder-agnostic attack engine and the six MITRE ATT&CK for ICS
scenarios for the CPS attack-dataset pipeline.  Runs on top of the Phase E
normal network events (`results/<feeder>/normal_<feeder>_network_events.csv`)
and the common grid model produced by `simulator.power`.

Pipeline position:

    Common Grid Model → Normal Simulation → Network/Event Layer (Phase E)
        → ⭐ Attack Engine (this package) → Attack Scenarios → Attack Dataset (Phase H)

## Non-goals (hard rules, unchanged from Phase E)

- No real DNP3/Modbus/IEC packets, no sockets, no real SCADA traffic.
  `protocol="DNP3"` is a logical label on every event.
- The physical telemetry and normal-scenario code are never modified by the
  attack layer.
- No hardcoded feeder id, bus number or point id anywhere in production code;
  targets are always discovered from the data actually given.
- No target → structured FAILED `AttackResult`, never a silent random pick.

## Modules

| module       | role                                                                 |
|--------------|----------------------------------------------------------------------|
| `base.py`    | `Attack` abstraction, `AttackContext`, `AttackActionResult`          |
| `engine.py`  | `AttackEngine.run(attack, context)` — lifecycle + `SCENARIOS` registry|
| `targets.py` | dynamic inventory (measurement + control points) + deterministic selection|
| `results.py` | `AttackResult` + `ground_truth()` / `event_rows()` for Task H         |
| `events.py`  | `AttackEvent` (Phase E schema + `injected`, `command_id`, …) + validation|
| `physical.py`| optional OpenDSS effect hook: baseline vs overridden solve deltas     |
| `mitre.py`   | verified MITRE ATT&CK for ICS technique catalog (T0846/T0855/T0836/T0856/T0803/T0804) |
| `scenarios/` | `reconnaissance`, `unauthorized_command`, `parameter_modification`, `false_measurement`, `communication_disruption`, `multi_step_attack` |

## Lifecycle (orchestrated by the engine)

```
context (scenario_id, feeder_id, model?, event_path/events?, timestamp)
  → build Inventory (measurements from normal events, controls from model)
  → validate_preconditions        unmet → FAILED(precondition_failed)
  → select_targets                none  → FAILED(no_compatible_target)
  → validate_target (exists/feeder/compatible/value)
  → execute → physical effect → events → AttackResult
```

`AttackResult.result ∈ {SUCCESS, FAILED}`; failures keep `failure_reason` and
the evaluated candidates so nothing is lost.

## Running the scenarios

```python
from simulator.power.loader import FeederLoader
from simulator.attack.engine import AttackEngine
from simulator.attack.base import AttackContext
from simulator.attack.scenarios import scenario_id_for, REGISTERED_SCENARIOS

model = FeederLoader().load("ieee37")
attack = REGISTERED_SCENARIOS["unauthorized_command"]
ctx = AttackContext(
    scenario_id=scenario_id_for(attack.attack_id, "ieee37"),
    feeder_id="ieee37",
    model=model,
    event_path="results/ieee37/normal_ieee37_network_events.csv",
    timestamp="2010-07-01T00:00:00",
)
result = AttackEngine(seed=42).run(attack, ctx)
print(result.report())
```

## Task H consumption surface

- `result.ground_truth()` — one flat ground-truth record (`is_attack`,
  `mitre_technique_ids`, target, original/injected/reported values, …).
- `result.event_rows()` — attack events as `ATTACK_EVENT_COLUMNS`-ordered rows.
- `simulator.attack.engine.SCENARIOS` — the executable attack registry.

## Running the scenarios (CLI)

```powershell
python -m simulator.attack --feeder ieee37                 # all scenarios
python -m simulator.attack --feeder ieee123 --attack unauthorized_command
```

See `AttackScenarios.md` for the full command list (env, scenarios, tests,
audit, real-data validation) and `Workflow.md` for the implementation record.

## Tests

    python3 -m unittest simulator.attack.test_attack_engine -v

50 tests: MITRE catalog, point/inventory/selection units, event model,
engine wiring, fail-closed behaviour, **future-feeder independence**, real-data
scenario execution on ieee37+ieee123, and (OpenDSS-gated) physical-effect
assertions.  All six scenarios must execute to SUCCESS on both feeders.

## Docs

- `README.md` — package quick-guide (this file)
- `Workflow.md` — implementation workflow / record of the work
- `AttackScenarios.md` — the six scenarios + command list
- `simulator/network/NetworkWorkflow.md` — Phase E baseline (+ Phase F/G section)