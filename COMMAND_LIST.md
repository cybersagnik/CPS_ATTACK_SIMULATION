# Command List & Guidance

Copy-paste command reference for the CPS Attack Simulation project.  Tier-by-tier
(Phases E -> F/G -> H) with test, audit and git commands.  Repo root is
`D:\CYBER_PHYSICAL\CPS_ATTACK_SIMULATION` (Windows / PowerShell).  All commands
are run from the repo root unless stated otherwise.

Quick index of entry points:

| phase | module | what it does |
|-------|--------|--------------|
| E | `python -m simulator.network.normal` | normal (attack-free) network-event CSV per feeder |
| F/G | `python -m simulator.attack` | run the six MITRE-ICS attack scenarios |
| H | `python -m simulator.dataset` | combined NORMAL+ATTACK dataset (CSV/JSON/manifest, fail-closed) |
| tests | `python -m pytest -q` | full suite = 103 passed, 2 skipped, 72 subtests |
| docs | files at repo root + `simulator/*/` | per-phase workflow and scenario reference |

## 1. Environment

```powershell
python --version                       # Python 3.14.7 (project baseline)
python -c "import opendssdirect; print(opendssdirect.__version__)"   # 0.9.4 (needed for physical solves)
git status                             # clean tree on branch main
```

Feeder model sanity check (loads the real OpenDSS model, prints a summary):

```powershell
python -c "from simulator.power.loader import FeederLoader; from simulator.power.system import System; m=FeederLoader().load('ieee37'); print(type(m).__name__, System(m)._summary())"
```

## 2. Phase E - normal network events

Requires the per-feeder telemetry CSVs + manifest under `results/<feeder>/`
(`results/ieee37/normal_ieee37_*telemetry*.csv` etc.); the generator joins and
validates them into one normal event CSV:

```powershell
python -m simulator.network.normal --feeder ieee37 --results-dir results/ieee37
python -m simulator.network.normal --feeder ieee123 --results-dir results/ieee123
```

Output: `results/<feeder>/normal_<feeder>_network_events.csv` (the source inputs
for Phases F/G/H).  These files are project inputs - do NOT modify them
(content hashes are audited, see section 6).

## 3. Phase F/G - attack scenarios (six scenarios, all through AttackEngine)

Run every registered scenario against a feeder:

```powershell
python -m simulator.attack --feeder ieee37      # all six, SUCCESS, exit 0
python -m simulator.attack --feeder ieee123
```

Run a single scenario (name from the registry / `--attack` choices):

```powershell
python -m simulator.attack --feeder ieee37 --attack false_measurement
python -m simulator.attack --feeder ieee123 --attack communication_disruption
python -m simulator.attack --feeder ieee37 --attack multi_step_attack
python -m simulator.attack --feeder ieee37 --attack parameter_modification
```

Selection/config knobs:

```powershell
python -m simulator.attack --feeder ieee37 --seed 7            # non-default deterministic seed
python -m simulator.attack --feeder ieee37 --modify-delta 0.50 # default 0.35
python -m simulator.attack --feeder ieee37 --timestamp 2010-07-01T00:00:00
```

The six registered scenarios and their MITRE mappings:

| `--attack` id | technique | tactic |
|---------------|-----------|--------|
| `reconnaissance` | T0846 Remote System Discovery | Discovery |
| `unauthorized_command` | T0855 Unauthorized Command Message | Impair Process Control |
| `parameter_modification` | T0836 Modify Parameter | Impair Process Control |
| `false_measurement` | T0856 Spoof Reporting Message | Evasion |
| `communication_disruption` | T0804 Block Reporting Message | Inhibit Response Function |
| `multi_step_attack` | T0846 + T0855 + T0836 | Discovery; Impair Process Control |

Phase F/G writes no files - persistence belongs to Task H.

## 4. Task H - combined dataset (deterministic, fail-closed)

```powershell
python -m simulator.dataset --feeders ieee37,ieee123 --seed 42 --events-dir results --results-dir results/dataset
```

Per feeder this generates, validates and exports, for **all six** registered
scenarios (the three extension scenarios are picked up automatically through the
`REGISTERED_SCENARIOS` registry - no exporter change needed per scenario):

- combined CSV `combined_<feeder>_dataset.csv` (canonical 25 columns),
- one attack-only CSV per scenario `attack_<feeder>_<attack_id>_dataset.csv`,
- ground-truth JSON `ground_truth_<feeder>.json` (full `AttackResult.as_dict()`
  per scenario; the multi-step `metadata["_stage_events"]` are serialised as
  event dicts, so the JSON is always loadable by `json`),
- manifest `manifest_<feeder>.json` with SHA-256 digests + a per-scenario
  `scenarios` record (attack id, feeder, status, event count, MITRE technique,
  output file).

Exit code 0 only if *every* feeder succeeds (fail-closed).  Verified run
results (seed 42):

```
OK   ieee37    combined=75152  normal=74592  attack=560   scenarios_ok=6
OK   ieee123   combined=157870 normal=156576 attack=1294  scenarios_ok=6
```

The three extension scenarios export 2 / 2 / 278 events (ieee37) and
2 / 2 / 645 events (ieee123) for false_measurement / communication_disruption /
multi_step_attack respectively.

## 5. Running the tests

Per-module:

```powershell
python -m unittest simulator.attack.test_attack_engine   -v   # 50 tests (attack scenarios + MITRE catalog + both feeders)
python -m unittest simulator.network.test_network_events -v   # 17 tests (Phase E)
python -m unittest simulator.dataset.test_dataset        -v   # 15 tests (Task H: schema + manifest + real CSV/JSON export)
python -m unittest simulator.power.test_dss_builder      -v   # DSS model builder
```

Single file / single test:

```powershell
python -m pytest -q simulator/attack/test_attack_engine.py
python -m pytest -q simulator/attack/test_attack_engine.py::test_all_registered_scenarios_execute_on_both_real_feeder_models
python -m pytest -q simulator/dataset/test_dataset.py::TaskHExportIntegrationTests
```

Full suite (run from repo root; `opendssdirect` present so physical tests execute):

```powershell
python -m pytest -q
# expected: 103 passed, 2 skipped, 72 subtests passed
# (2 skips are the OpenDSS-unavailable guards; stderr "C stack trace" at exit is pre-existing Windows noise)
```

## 6. Audits & verification

Source-input integrity (must be unchanged):

```powershell
Get-FileHash -Algorithm SHA256 results/ieee37/normal_ieee37_network_events.csv
Get-FileHash -Algorithm SHA256 results/ieee123/normal_ieee123_network_events.csv
# expected SHA-256:
#   ieee37:  c547d868923199d1379da4df9d53b43bf3ba3a60a9ae1452ced8813fdc3c6b62
#   ieee123: 59aa86cec48479a5f408ec62263921e08c70530145562524ed6fac6a47a4a3aa
```

No hardcoded feeder/bus/point ids in production attack code (only docstring
examples allowed):

```powershell
python -c "import re,pathlib; hits=[(p,l) for p in pathlib.Path('simulator/attack').rglob('*.py') if 'test_' not in p.name for l in p.read_text(encoding='utf-8').splitlines() if re.search(r'ieee37|ieee123|BUS_[0-9]|CURRENT_[a-z]', l)]; print('hits:', len(hits)); [print(p,'|',l.strip()) for p,l in hits]"
# expected: 0 executable-logic hits (a few docstring usage examples are fine)
```

Determinism (seed 42): run any scenario twice and diff; outputs must be identical.

Verify a Task H export (files, scenario ids, MITRE, chronology, ground truth):

```powershell
python -m simulator.dataset --feeders ieee37,ieee123 --seed 42 --events-dir results --results-dir $env:TEMP\cps_ds_verify
Get-ChildItem $env:TEMP\cps_ds_verify | Where-Object { $_.Name -match 'false_measurement|communication_disruption|multi_step_attack' } | Select-Object Name, Length
python -c "import json; gt=json.load(open(r'$env:TEMP\cps_ds_verify\ground_truth_ieee37.json', encoding='utf-8')); print([ (r['attack_id'], r['event_count'], r['mitre_technique_ids']) for r in gt['attacks'] ])"
python -c "import json; m=json.load(open(r'$env:TEMP\cps_ds_verify\manifest_ieee123.json', encoding='utf-8')); print([ (s['attack_id'], s['status'], s['event_count'], s['mitre_technique']) for s in m['scenarios'] ])"
```

## 7. Windows / WSL notes

- This terminal is Windows PowerShell 5.1 - use `;` (or `if ($?) {...}`) to
  chain, never `&&`.  Native command args with spaces need surrounding quotes.
- `pytest` under this PowerShell emits a harmless `C stack trace` at shutdown.
- Long Python snippets in PowerShell: use a literal here-string written to a
  temp file `C:\Users\...\AppData\Local\Temp\opencode\`, then `python <file>`.
- WSL mirror (`wsl -d Ubuntu -e bash -lc '...'`) can re-run pytest/grep from
  `/mnt/d/CYBER_PHYSICAL/CPS_ATTACK_SIMULATION` for cross-checks.

## 8. Git workflow

```powershell
git status                    # review what changed
git diff --stat               # spread of the change
git log --oneline -5          # match commit-message style
git add -A                    # stage all intended changes
git commit -m "Add ..."       # repo-local identity: Abhishek Kumar Gupta <abhishekkumargupta20020@gmail.com>
git push                      # to origin/main
```

Rules: commit/push only when explicitly asked; never commit source datasets
(`results/*/normal_*`), secrets, or node modules; keep commit messages short and
matching repo style (e.g. "Add UPDATES.md, update README").  The repo-local
`user.name` / `user.email` are set to `Abhishek Kumar Gupta /
abhishekkumargupta20020@gmail.com` so commits are attributed to that GitHub
account rather than the previous `Debonmoy <debonmoy.pal.bwn@gmail.com>` one.