# Network Layer Workflow (simulator/network)

Canonical documentation for the network / event layer of the Phase 1 CPS
attack-dataset pipeline.  From now on all network-layer workflow — normal
baseline, attack/event layer behaviour, schemas, validation runs and dataset
result details — is recorded here (not in `grid_data/workflow.md`).

Pipeline position:

    Feeder Model + Ausgrid Data  →  Common Grid Model  →  Normal Simulation
        →  ⭐ Network / Event Layer (this package)  →  Attack Engine  →  Dataset

The network layer observes the physical simulation through the logical SCADA
dialogs that would collect its telemetry.  It is a pure consumer: it never
reruns OpenDSS, never opens sockets, and never rewrites the physical telemetry
files it reads.

---

## Phase E — Normal Network Events (Baseline)

### 1. Purpose

Insert the *network* (SCADA / field-device) observability layer between the
normal simulation and the later attack phase.  Each normal run's physical
telemetry is consumed to produce a deterministic, protocol-neutral
network-event dataset: `results/<feeder>/normal_<feeder>_network_events.csv`.

### 2. Scope / non-goals

- No attack logic, no injected/replayed events, no bad quality / dropped
  delivery / non-zero latency (those belong to a later attack phase).
- No real protocol implementation: `protocol="DNP3"` is a logical label only.
  No sockets, packets, Modbus, IEC 61850/60870-5-104, TLS or IEC 62351.
- Devices are exactly two logical endpoints: `SCADA_MASTER` and `RTU_<feeder_id>`.
- No per-bus RTUs and no network topology invention.

### 3. Inputs / outputs

Reads (from a `results/<feeder>/` directory):

- `normal_<feeder>_bus_telemetry.csv`     (`timestamp,feeder_id,bus,vpu_AB,vpu_BC,vpu_CA`)
- `normal_<feeder>_current_telemetry.csv` (`timestamp,feeder_id,element,element_type,bus,phase,i_amps`)
- `normal_<feeder>_summary.csv`           (`...,source_p_kw,source_q_kvar,converged,n_buses,n_deenergized,vpu_min,vpu_max`)
- `normal_<feeder>_manifest.json`         (scenario id, feeder id, window/steps)

Writes:

- `normal_<feeder>_network_events.csv` (one POLL per timestamp, then that
  timestamp's TELEMETRY measurements).

### 4. Event model

Each event row has the fixed schema (column order):

    event_id,scenario_id,timestamp,feeder_id,src_device,dst_device,protocol,
    message_type,direction,sequence,point_id,value,unit,quality,delivery_status,latency_ms

Message types / directions:

| message  | src → dst                       | direction       | payload                       |
|----------|----------------------------------|-----------------|-------------------------------|
| POLL     | SCADA_MASTER → RTU_<feeder>     | SCADA_TO_RTU    | none (point_id/value empty)   |
| TELEMETRY| RTU_<feeder> → SCADA_MASTER     | RTU_TO_SCADA    | one measurement point         |

Normal-phase constants: `protocol=DNP3`, `quality=GOOD`, `delivery_status=DELIVERED`,
`latency_ms=0`.  Timestamps are copied verbatim from the physical telemetry
(naive local); network time == physical time, no timezone shift.

### 5. Point-id encoding (deterministic, guaranteed)

| source telemetry | point id pattern                            | unit |
|------------------|---------------------------------------------|------|
| bus telemetry    | `BUS_<bus>_VPU_<AB|BC|CA>`                  | pu   |
| current telemetry| `CURRENT_<element_type>_<element>_<phase>`  | A    |
| summary          | `FEEDER_SOURCE_P` / `FEEDER_SOURCE_Q`       | kW / kvar |
| summary          | `FEEDER_VPU_MIN` / `FEEDER_VPU_MAX`         | pu   |
| summary          | `FEEDER_CONVERGED`                          | (bool) |

`<element>` / `<element_type>` are sanitized to `[A-Za-z0-9_]` (`.`/`-` → `_`).
Empty telemetry cells are skipped (e.g. single-phase buses have no line-to-line
pu; `summary.vpu_min/max` may be blank for some steps).  `value` is copied from
the source cell unmodified; event rows are ordered `timestamp → message_type →
src/dst → point_id`, POLL before its TELEMETRYs.

### 6. Identity

`sequence` is a monotonic integer restarting at 1 per dataset file;
`event_id = "<scenario_id>-ev<sequence:08d>"` (e.g. `normal_ieee37-ev00000001`).
Both are assigned by `NetworkEventRecorder` in deterministic output order, so
identity is a pure function of the physical telemetry.

### 7. Files

- `simulator/network/__init__.py`
- `simulator/network/events.py` — `NetworkEvent` model, validation, point-id /
  device constants
- `simulator/network/normal.py` — `NormalNetworkGenerator` (streaming
  timestamp-merge over the three telemetry CSVs), `NetworkEventRecorder`
  (validate / sort / identity / write), CLI
- `simulator/network/test_network_events.py` — 17 unit tests (synthetic
  fixtures only)
- `simulator/network/README.md` — package quick-guide
- Outputs: `results/ieee37/normal_ieee37_network_events.csv`,
  `results/ieee123/normal_ieee123_network_events.csv`

### 8. CLI usage

    python3 -m simulator.network.normal --feeder ieee37  --results-dir results/ieee37
    python3 -m simulator.network.normal --feeder ieee123 --results-dir results/ieee123
    python3 -m simulator.network.normal --feeder ieee37 --results-dir results/ieee37 --out /tmp/out.csv

### 9. Unit tests

`simulator/network/test_network_events.py` (17 tests, no OpenDSS/xlrd/registry
needed) covers: event construction/defaults, field validation (bad message type,
direction flip, wrong src/dst, missing value, empty point_id, non-GOOD quality,
non-DELIVERED delivery, non-zero latency, non-numeric/NaN value), POLL shape
enforcement, per-timestamp POLL first, bus voltage mapping incl. single-phase
skips, current mapping, summary mapping, value equality with physical
telemetry, timestamp preservation, deterministic contiguous sequence numbers,
deterministic unique event ids, CSV header/schema, byte-identical repeated
generation, recorder rejects out-of-order batches, and missing/malformed
source handling (missing manifest/current CSV; unparseable vpu).

### 10. Real-data validation

- `ieee37`: 336 timestamps, 74 592 events (336 POLL + 74 256 TELEMETRY):
  36 288 bus, 36 288 current, 1 680 summary points.  Every TELEMETRY event's
  `value` equals the source telemetry value exactly (1e-12 tolerance), and
  every source measurement point maps to exactly one event.
- `ieee123`: 336 timestamps, 156 576 events (336 POLL + 156 240 TELEMETRY):
  67 536 bus, 87 024 current, 1 680 summary points.  Same value/mapping
  checks pass (ieee123's single-phase buses with blank `vpu_*` cells are
  correctly skipped, e.g. bus 10).
- Determinism: rerunning each feeder produces a byte-identical file.
- Source immutability: sha256 of all 8 telemetry/manifest source files is
  unchanged after generation.

### 11. Future network-layer phases

The event schema above is the normal baseline.  Planned network-layer phases
will extend it (never in-place) with attack-specific fields such as `injected`,
`replay_of_event_id`, `original_value`, `reported_value`, `command_id`,
`result`, and non-normal `quality` / `delivery_status` / `latency_ms` values.
Each new network-layer phase gets its own section under this file.

## Phase F/G — Attack Engine & MITRE-ICS Scenarios (simulator/attack)

The network layer stays the canonical observability layer.  The attack phase
(`simulator/attack`) is a **pure consumer that composes new tooling over the
Phase E event model** — it never rewrites `normal.py`, the physical telemetry
or the event schema.  Phase E rules (no sockets, no real DNP3 — `protocol`
stays a logical `DNP3` label) remain binding for every attack event.

### 1. Event-model extension

Attack events reuse the Phase E schema **plus** the extension fields planned in
Phase E §11 (column order, appended unmodified):

    event_id,scenario_id,timestamp,feeder_id,src_device,dst_device,protocol,
    message_type,direction,sequence,point_id,value,unit,quality,delivery_status,
    latency_ms,injected,replay_of_event_id,original_value,reported_value,
    command_id,result

New *logical* message types (still protocol-neutral):

| message  | src → dst                   | direction       | payload                                                        |
|----------|-----------------------------|-----------------|----------------------------------------------------------------|
| QUERY    | SCADA_MASTER → RTU_<feeder> | SCADA_TO_RTU    | none (point_id/value empty) — reconnaissance interrogation     |
| COMMAND  | SCADA_MASTER → RTU_<feeder> | SCADA_TO_RTU    | one control point, numeric `value`, `command_id` non-empty     |
| REPORT   | RTU_<feeder> → SCADA_MASTER | RTU_TO_SCADA    | one point read-back / command ack, numeric `value`             |

Non-normal `quality`/`delivery_status`/`latency_ms` are allowed only when a
future attack scenario requires them; the three shipped scenarios keep
GOOD/DELIVERED/0 because the transport itself is unharmed — it is intent or an
injected value that is malicious.

`injected` semantics (`simulator/attack/events.py` docstring is canonical):

- `injected=1` — message did not originate from the Phase E normal dialog:
  attacker-forged `COMMAND` / `QUERY` messages, and any `REPORT` carrying a
  value the operator cannot trust (data forgery, e.g. a tampered parameter
  reported as the operating value).
- `injected=0` — genuine device echo of a *true* post-action state (e.g. the
  RTU honestly acknowledging an applied unauthorized command).

Attack event identity: `event_id = "<scenario_id>-atk<NNN>"` (`NNN` restarts
at 001 per scenario run); scenario id follows the Phase E convention
`<attack_id>_<feeder_id>` (e.g. `unauthorized_command_ieee37`).

### 2. Package layout (simulator/attack)

- `base.py` — `Attack` abstraction + `AttackContext` + `AttackActionResult`
- `engine.py` — `AttackEngine.run(attack, context)` orchestrator (always
  returns a structured `AttackResult`; never attacks silently)
- `targets.py` — dynamic, feeder-agnostic discovery + deterministic selection
- `results.py` — `AttackResult` / `ground_truth()` / `event_rows()`
- `events.py` — `AttackEvent` (Phase E schema + extension) + validation
- `physical.py` — optional OpenDSS physical-effect hook (baseline vs
  overridden solve deltas)
- `mitre.py` — verified MITRE ATT&CK for ICS catalog
- `scenarios/` — the three shipped scenario implementations
- `engine.SCENARIOS` + `scenarios.REGISTERED_SCENARIOS` — scenario registry

### 3. Shipped scenarios (Task G) with MITRE mappings

| scenario / attack_id        | MITRE technique                       | tactic                |
|-----------------------------|---------------------------------------|-----------------------|
| `reconnaissance`            | T0846 Remote System Discovery         | Discovery (TA0102)    |
| `unauthorized_command`      | T0855 Unauthorized Command Message    | Impair Process Control (TA0106) |
| `parameter_modification`    | T0836 Modify Parameter                | Impair Process Control (TA0106) |

Technique names/tactics were verified against the official ATT&CK for ICS
source (`https://attack.mitre.org`); see `mitre.py` for the rationale recorded
per technique.  Target selection is dynamic and feeder-agnostic (no hardcoded
feeder/bus/device ids in production code; the engine also runs unmodified on a
hypothetical unknown feeder — see test `FutureFeederIndependenceTests`).  When
no compatible target exists, the engine returns a structured FAILED `AttackResult`
— it never invents a target.

### 4. Dataset surface for Task H (exporter)

- `result.as_dict()` — one flat ground-truth record per attack execution.
- `result.event_rows()` — the attack network events as row dicts in the
  `ATTACK_EVENT_COLUMNS` order (appendable to the normal events CSV).
- `engine.SCENARIOS` — available attacks to export.

### 5. Validation

    python3 -m unittest simulator.network.test_network_events   # Phase E (must stay green)
    python3 -m unittest simulator.attack.test_attack_engine -v  # attack engine + scenarios

Real-data coverage: all three scenarios × {ieee37, ieee123} run to SUCCESS on
the real models + real `normal_*_network_events.csv`, every generated event
passes `validate_attack_event`, sequences are contiguous, physical effect
deltas are non-degenerate, and Phase E tests remain green.

---

**PHASE E COMPLETE + PHASE F (ATTACK ENGINE) & PHASE G (MITRE SCENARIOS) DONE.
NEXT = PHASE H (ATTACK DATASET EXPORT — consumes `AttackResult`).**