# simulator/network — Network / Event Layer (Phase E)

Consumes a feed's *normal* physical simulation outputs and produces the
network-event dataset for it.  Everything here is protocol-neutral and
deterministic: it never builds real DNP3/Modbus packets, never opens sockets,
and never rewrites the physical telemetry it reads (`protocol="DNP3"` is a
logical label only).

> **Workflow docs:** `NetworkWorkflow.md` in this directory is the canonical
> network-layer workflow document (event model, point-id encoding, CLI,
> unit tests, real-data validation).  Keep all network-layer workflow updates
> there from now on.

## Modules

- `events.py` — `NetworkEvent` model, fields, validation, and the fixed
  `NETWORK_EVENT_COLUMNS` schema; device (`SCADA_MASTER`, `RTU_<feeder>`),
  message-type (`POLL` / `TELEMETRY`), direction (`SCADA_TO_RTU` /
  `RTU_TO_SCADA`) and normal-phase quality/delivery/latency constants.
- `normal.py` — `NormalNetworkGenerator` (streams the three telemetry CSVs,
  merging them timestamp-by-timestamp) and `NetworkEventRecorder` (validates,
  deterministically orders, assigns `sequence` / `event_id`, writes the CSV).

## CLI

    python3 -m simulator.network.normal --feeder ieee37 --results-dir results/ieee37

Writes `results/<feeder>/normal_<feeder>_network_events.csv` (override with
`--out`).

## Inputs / outputs

- Reads (from a `results/<feeder>/` dir): `normal_<feeder>_bus_telemetry.csv`,
  `normal_<feeder>_current_telemetry.csv`, `normal_<feeder>_summary.csv`,
  `normal_<feeder>_manifest.json`.
- Writes: `normal_<feeder>_network_events.csv` with one POLL per timestamp
  followed by that timestamp's TELEMETRY measurements, ordered
  `timestamp → message_type → src/dst → point_id`.
- Point ids: `BUS_<bus>_VPU_<AB|BC|CA>` (pu), `CURRENT_<element_type>_<element>_<phase>` (A),
  `FEEDER_SOURCE_P` (kW), `FEEDER_SOURCE_Q` (kvar), `FEEDER_VPU_MIN`/`FEEDER_VPU_MAX` (pu),
  `FEEDER_CONVERGED`.  Empty cells are skipped; `value` is copied from the
  source cell unmodified.

## Tests

    python3 -m unittest simulator.network.test_network_events -v

No OpenDSS, xlrd, or feeder registry required.