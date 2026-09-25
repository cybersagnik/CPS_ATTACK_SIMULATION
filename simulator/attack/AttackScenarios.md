# Attack Scenarios (simulator/attack) — Task G

The six MITRE ATT&CK for ICS scenarios implemented for the attack-phase
dataset.  Each is a proper `Attack` implementation running through the shared
`AttackEngine` — never a stand-alone script.  Scenario ids follow the Phase E
convention: `<attack_id>_<feeder_id>` (e.g. `unauthorized_command_ieee37`).

## 1. Scenario table

| attack_id | name | MITRE technique | tactic | what it does |
|-----------|------|-----------------|--------|--------------|
| `reconnaissance` | Reconnaissance / Remote System Discovery | T0846 Remote System Discovery | Discovery (TA0102) | Interrogates the feeder RTU: QUERY dialogs + one REPORT per discovered measurement point & numeric control parameter; returns the enumerated surface (endpoints, point kinds, buses, elements, control surface). |
| `unauthorized_command` | Unauthorized Command | T0855 Unauthorized Command Message | Impair Process Control (TA0106) | Issues an unauthorized state-change COMMAND to a discovered device — prefer `switch.closed`, else `capacitor.switched`, else `load.enabled` (cascade); commanded state is the opposite of the legitimate current state; physical deltas computed when the solver is present. |
| `parameter_modification` | Parameter Modification | T0836 Modify Parameter | Impair Process Control (TA0106) | Modifies a discovered load `kw_multiplier`/`kvar_multiplier` to `baseline + delta` (default delta 0.35 → 1.35), reporting the tampered value as the operating value; physical effect (voltage/power deltas) computed. |
| `false_measurement` | False Measurement | T0856 Spoof Reporting Message | Evasion (TA0103); also Impair Process Control (TA0106) | Forges the RTU→SCADA reporting message of a real measurement point: reported = actual × `measurement_scale` (default 1.05). The grid is untouched — physical deltas are the honest zero — while the operator sees the wrong value (cyber and physical truth differ). |
| `communication_disruption` | Communication Disruption | T0804 Block Reporting Message | Inhibit Response Function (TA0107) | Blocks the genuine reporting message of a measurement point: `delivery_status=DROPPED`. The grid keeps operating (zero physical deltas) but the operator loses visibility (message loss). |
| `multi_step_attack` | Multi-Step Attack | T0846 + T0855 + T0836 | Discovery; Impair Process Control | Composes reconnaissance → unauthorized_command → parameter_modification into one chronological timeline (stage timestamps t, t+1s, t+2s) sharing a single `scenario_id`. The combined physical footprint is the union of every modifying stage's overrides; per-stage effects are preserved in metadata. |

Technique names/tactics were verified against the official ATT&CK for ICS
source (v15.1, `https://attack.mitre.org/versions/v15/techniques`) and the CISA
technique entries; the catalog with per-technique rationale lives in `mitre.py`.

## 2. Per-scenario event footprints

| scenario | messages produced | injected flag |
|----------|-------------------|---------------|
| reconnaissance | 2 × QUERY (SCADA_MASTER → RTU) + N× REPORT (RTU → SCADA, one per discovered numeric point) | QUERY = 1; REPORT echoes = 0 (true device data) |
| unauthorized_command | 1 × COMMAND + 1 × REPORT (ack) | COMMAND = 1 (forged); REPORT = 0 (honest ack) |
| parameter_modification | 1 × COMMAND + 1 × REPORT (tampered value reported as operating value) | COMMAND = 1; REPORT = 1 (data forgery) |
| false_measurement | 1 × QUERY + 1 × forged REPORT | both = 1 (forged read dialog + forged report) |
| communication_disruption | 1 × QUERY + 1 × REPORT with `delivery_status=DROPPED` | QUERY = 1 (block instruction); REPORT = 0 (genuine measurement, never delivered) |
| multi_step_attack | recon footprint + (1 × COMMAND + 1 × REPORT) + (1 × COMMAND + 1 × REPORT) | per composed stage, exactly as its single-step footprint |

`event_id = <scenario_id>-atk<NNN>`, assigned contiguously by the engine across
the whole timeline, so a multi-step scenario yields one numbered sequence.
Example footprints on the real data: `reconnaissance_ieee37` → 274 events;
`reconnaissance_ieee123` → 641; `multi_step_attack_ieee37` → 278;
`multi_step_attack_ieee123` → 645; `false_measurement` / 
`communication_disruption` → 2 events each.

## 3. Fail-closed behaviour

Each scenario is structurally unable to produce a silent/random attack.  The
engine returns a structured `FAILED` result at the first unsound stage:

- precondition failure (e.g. no controllable-asset model for a command);
- no compatible target (e.g. Unauthorized Command on a feeder with no
  switch/capacitor/load) →  `no_compatible_target` + evaluated candidates;
- selected target invalid (missing point / wrong feeder / incompatible) →
  `target_invalid`.

The multi-step scenario additionally verifies the composite preconditions of
*all three* of its stages before executing any of them — it fails cleanly at
the precondition stage if, for example, the feeder has no physically supported
load coefficient.

## 4. Command list

Run from the repository root (`D:\CYBER_PHYSICAL\CPS_ATTACK_SIMULATION`).

### 4.1 Environment (one-time)

```powershell
python -m pip install PyYAML xlrd==1.2.0 pytest "opendssdirect.py==0.9.4"
```

### 4.2 Scenario CLI — run every scenario on a feeder

```powershell
# all scenarios on ieee37
python -m simulator.attack --feeder ieee37

# all scenarios on ieee123
python -m simulator.attack --feeder ieee123

# a single attack on ieee123
python -m simulator.attack --feeder ieee123 --attack unauthorized_command

# the new scenarios individually
python -m simulator.attack --feeder ieee37 --attack false_measurement
python -m simulator.attack --feeder ieee37 --attack communication_disruption
python -m simulator.attack --feeder ieee37 --attack multi_step_attack

# custom seed + modification delta
python -m simulator.attack --feeder ieee37 --seed 7 --modify-delta 0.50
```

`--attack` choices are discovered from the scenario registry, so the three new
attacks appear automatically.  Prints one structured `AttackResult` report per
scenario (`result`, `target`, `mitre`, `events`, `physical_effect deltas`).
Exits non-zero if any scenario reports FAILED.  Does not write any file (Task H).

### 4.3 Test suite

```powershell
# attack engine + scenarios (50 tests)
python -m unittest simulator.attack.test_attack_engine -v

# Phase E regression (must stay green — 17 tests)
python -m unittest simulator.network.test_network_events -v

# whole repository (pytest collects everything)
python -m pytest -q
```

### 4.4 Feeder-hardcoding audit

```powershell
# Scan production code for literal ids. Expected output: ONLY module-docstring
# usage examples (documentation) and generic pattern prefixes such as
# point_id.startswith("BUS_") -- both are fine. Zero executable-logic hits
# allowed: no hardcoded feeder/bus/device/point ids in scenario logic.
python -c "import re,pathlib; [print(p, l) for p in pathlib.Path('simulator/attack').rglob('*.py') if 'test_' not in p.name for l in p.read_text(encoding='utf-8').splitlines() if re.search(r'ieee37|ieee123|BUS_[0-9]|CURRENT_[a-z]', l)]"
```

### 4.5 Real-data validation

```powershell
# end-to-end runs on the real models + real normal network events
python -m simulator.attack --feeder ieee37
python -m simulator.attack --feeder ieee123
```

Both must end with `result: SUCCESS` for all six scenarios.  `physical_effect
deltas` are computed when `opendssdirect` is installed: non-zero for
`unauthorized_command` / `parameter_modification` / `multi_step_attack` (the
multi-step one is the *combined* footprint of all its modifying stages), and
the honest zero for `false_measurement` / `communication_disruption` — the 
physical truth is unchanged in those two by design.