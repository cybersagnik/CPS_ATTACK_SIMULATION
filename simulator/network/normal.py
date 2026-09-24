"""Phase E normal network-event dataset generator.

Consumes a completed *normal* simulation's physical telemetry -- the bus /
current telemetry CSVs, the summary CSV and the run manifest that
``simulator.power.normal_scenario`` wrote into a results directory -- and
produces a deterministic, protocol-neutral network-event dataset
``normal_<feeder>_network_events.csv``.

It is a downstream *consumer* of the physical simulation, never a producer: it
does not rerun OpenDSS, and it never rewrites the telemetry files it reads.
``protocol="DNP3"`` is a logical label only; no protocol packets are built.

Generation model
----------------
* Devices are ``SCADA_MASTER`` and ``RTU_<feeder_id>`` only.
* One ``POLL`` (SCADA_MASTER -> RTU) precedes that timestamp's ``TELEMETRY``
  (RTU -> SCADA_MASTER) events; telemetry carries one measurement per point.
* Measurements are mapped 1:1 from the physical telemetry with deterministic
  point ids (see below) and ``value`` copied verbatim (no recalculation).
* Timestamps are the physical telemetry timestamps, unchanged (naive local).
* The three telemetry CSVs are merged in streaming fashion, timestamp by
  timestamp, so event construction never holds a whole year in memory.

Point-id encoding (deterministic)
---------------------------------
``BUS_<bus>_VPU_<AB|BC|CA>``      -- bus line-to-line per-unit voltage (pu).
``CURRENT_<ELEMENT_TYPE>_<elem>_<A|B|C>``  -- element phase current (A), with
``<elem>`` the source element name sanitized to ``[A-Za-z0-9_]``.
``FEEDER_<SOURCE_P|SOURCE_Q|VPU_MIN|VPU_MAX|CONVERGED>`` -- summary values
(kW / kvar / pu / pu / dimensionless) when the summary CSV carries them.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import itertools
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .events import (
    DEVICE_SCADA_MASTER,
    DIRECTION_RTU_TO_SCADA,
    DIRECTION_SCADA_TO_RTU,
    MESSAGE_TYPE_POLL,
    MESSAGE_TYPE_TELEMETRY,
    NETWORK_EVENT_COLUMNS,
    NetworkEvent,
    NetworkEventError,
    rtu_of,
    validate_normal_event,
)

BUS_POINT_IDS = ("BUS_{bus}_VPU_AB", "BUS_{bus}_VPU_BC", "BUS_{bus}_VPU_CA")
BUS_UNITS = ("pu", "pu", "pu")
BUS_COLUMNS = ("vpu_AB", "vpu_BC", "vpu_CA")

#: (summary column, point id, unit) in summary output order for telemetry rows.
SUMMARY_POINTS = (
    ("source_p_kw", "FEEDER_SOURCE_P", "kW"),
    ("source_q_kvar", "FEEDER_SOURCE_Q", "kvar"),
    ("vpu_min", "FEEDER_VPU_MIN", "pu"),
    ("vpu_max", "FEEDER_VPU_MAX", "pu"),
    ("converged", "FEEDER_CONVERGED", ""),
)

_REQUIRED_BUS_COLUMNS = ("timestamp", "feeder_id", "bus", "vpu_AB", "vpu_BC", "vpu_CA")
_REQUIRED_CURRENT_COLUMNS = (
    "timestamp", "feeder_id", "element", "element_type", "bus", "phase", "i_amps",
)
_REQUIRED_SUMMARY_COLUMNS = ("timestamp", "feeder_id")


def _safe_token(name: str) -> str:
    """Element-name -> CSV-safe token (``[A-Za-z0-9_]``)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _parse_number(raw: str, where: str) -> float:
    """Parse a telemetry cell to float, wrapping parse failures cleanly."""
    try:
        value = float(raw)
    except ValueError as exc:
        raise NetworkEventError(f"unparseable {where}: {raw!r}") from exc
    if not math.isfinite(value):
        raise NetworkEventError(f"non-finite {where}: {raw!r}")
    return value


def _iter_timestamp_groups(path: Path) -> Iterator[Tuple[str, List[Dict[str, str]]]]:
    """Yield (timestamp, rows) groups, assuming the file is timestamp-sorted.

    Streaming: only one timestamp's rows are materialised at a time.  The
    normal-scenario telemetry writers emit rows grouped by timestamp, so this
    holds for the produced files; callers must never rely on it for arbitrary
    user CSVs (only the merge step, below, depends on grouping).
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for timestamp, rows in itertools.groupby(reader, key=lambda r: r["timestamp"]):
            yield timestamp, list(rows)


def _iter_merged_timestamps(paths: Iterable[Tuple[str, Path]]) -> Iterator[Tuple[str, Dict[str, List[Dict[str, str]]]]]:
    """Merge several timestamp-sorted telemetry CSVs into per-timestamp groups.

    Yields ``(timestamp, {source: rows})`` in ascending timestamp order using a
    heap over per-file group iterators, so memory stays bounded by one timestamp
    per file.  Rows for the same timestamp are grouped per source.
    """
    sources = list(paths)
    index_by_name = {name: i for i, (name, _) in enumerate(sources)}
    generators: List[Iterator[Tuple[str, List[Dict[str, str]]]]] = []
    for source_name, path in sources:
        if not path.is_file():
            raise NetworkEventError(f"missing telemetry file: {path}")
        generators.append(_iter_timestamp_groups(path))

    heap: List[Tuple[str, int, List[Dict[str, str]]]] = []
    for index, generator in enumerate(generators):
        try:
            timestamp, rows = next(generator)
            heapq.heappush(heap, (timestamp, index, rows))
        except StopIteration:
            continue

    while heap:
        timestamp, index, rows = heapq.heappop(heap)
        grouped: Dict[str, List[Dict[str, str]]] = {sources[index][0]: rows}
        while heap and heap[0][0] == timestamp:
            _, peer_index, peer_rows = heapq.heappop(heap)
            grouped[sources[peer_index][0]] = peer_rows
        for contributor in grouped:
            try:
                next_ts, next_rows = next(generators[index_by_name[contributor]])
            except StopIteration:
                continue
            heapq.heappush(heap, (next_ts, index_by_name[contributor], next_rows))
        yield timestamp, grouped


class NormalNetworkGenerator:
    """Generate normal network events from an existing normal run's telemetry."""

    def __init__(self, results_dir: Path, feeder_id: str) -> None:
        self.results_dir = Path(results_dir)
        self.feeder_id = feeder_id
        self.manifest = self._read_manifest()
        scenario = str(self.manifest.get("scenario") or "normal")
        self.scenario_id = f"{scenario}_{feeder_id}"
        self.paths = {
            "bus": self.results_dir / f"normal_{feeder_id}_bus_telemetry.csv",
            "current": self.results_dir / f"normal_{feeder_id}_current_telemetry.csv",
            "summary": self.results_dir / f"normal_{feeder_id}_summary.csv",
        }
        self._validate_headers()

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _read_manifest(self) -> Dict[str, object]:
        manifest_path = self.results_dir / f"normal_{self.feeder_id}_manifest.json"
        if not manifest_path.is_file():
            raise NetworkEventError(f"manifest not found: {manifest_path}")
        with open(manifest_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _validate_headers(self) -> None:
        def header(path: Path) -> List[str]:
            with open(path, newline="", encoding="utf-8") as fh:
                return next(csv.reader(fh))

        checks = (self.paths["bus"], _REQUIRED_BUS_COLUMNS), (self.paths["current"], _REQUIRED_CURRENT_COLUMNS)
        for path, required in checks:
            if not path.is_file():
                raise NetworkEventError(f"missing telemetry file: {path}")
            columns = header(path)
            missing = [c for c in required if c not in columns]
            if missing:
                raise NetworkEventError(f"{path.name}: missing required columns {missing}")

    # ------------------------------------------------------------------ #
    # per-timestamp event construction (feeder-agnostic)
    # ------------------------------------------------------------------ #
    def _bus_events(self, rows: List[Dict[str, str]]) -> List[NetworkEvent]:
        events: List[NetworkEvent] = []
        for row in rows:
            bus = row["bus"]
            for pair, unit, column in zip(BUS_POINT_IDS, BUS_UNITS, BUS_COLUMNS):
                raw = row.get(column, "").strip()
                if not raw:
                    continue  # e.g. single-phase bus has no line-to-line pu
                value = _parse_number(raw, f"vpu bus {bus!r} {column}")
                events.append(NetworkEvent(
                    scenario_id=self.scenario_id,
                    timestamp=row["timestamp"],
                    feeder_id=self.feeder_id,
                    src_device=rtu_of(self.feeder_id),
                    dst_device=DEVICE_SCADA_MASTER,
                    message_type=MESSAGE_TYPE_TELEMETRY,
                    direction=DIRECTION_RTU_TO_SCADA,
                    point_id=pair.format(bus=bus),
                    value=value,
                    unit=unit,
                ))
        return events

    def _current_events(self, rows: List[Dict[str, str]]) -> List[NetworkEvent]:
        events: List[NetworkEvent] = []
        for row in rows:
            element = _safe_token(row["element"])
            element_type = _safe_token(row["element_type"])
            phase = row["phase"]
            raw = row.get("i_amps", "").strip()
            if not raw:
                continue
            value = _parse_number(raw, f"i_amps for {row['element']!r}/{phase}")
            events.append(NetworkEvent(
                scenario_id=self.scenario_id,
                timestamp=row["timestamp"],
                feeder_id=self.feeder_id,
                src_device=rtu_of(self.feeder_id),
                dst_device=DEVICE_SCADA_MASTER,
                message_type=MESSAGE_TYPE_TELEMETRY,
                direction=DIRECTION_RTU_TO_SCADA,
                point_id=f"CURRENT_{element_type}_{element}_{phase}",
                value=value,
                unit="A",
            ))
        return events

    def _summary_events(self, rows: List[Dict[str, str]]) -> List[NetworkEvent]:
        events: List[NetworkEvent] = []
        for row in rows:
            for column, point_id, unit in SUMMARY_POINTS:
                raw = row.get(column, "").strip()
                if not raw:
                    continue  # field not emitted / not present for this run
                value = _parse_number(raw, f"summary {column}")
                events.append(NetworkEvent(
                    scenario_id=self.scenario_id,
                    timestamp=row["timestamp"],
                    feeder_id=self.feeder_id,
                    src_device=rtu_of(self.feeder_id),
                    dst_device=DEVICE_SCADA_MASTER,
                    message_type=MESSAGE_TYPE_TELEMETRY,
                    direction=DIRECTION_RTU_TO_SCADA,
                    point_id=point_id,
                    value=value,
                    unit=unit,
                ))
        return events

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def iter_timestamp_groups(self) -> Iterator[Tuple[str, List[NetworkEvent]]]:
        """Yield ``(timestamp, events)`` with one POLL plus the mapped TELEMETRYs.

        Events of each timestamp are returned in deterministic point order; the
        :class:`NetworkEventRecorder` re-sorts them anyway, so this order is a
        convenience, not a contract.
        """
        for timestamp, grouped in _iter_merged_timestamps(self.paths.items()):
            events: List[NetworkEvent] = [
                NetworkEvent(
                    scenario_id=self.scenario_id,
                    timestamp=timestamp,
                    feeder_id=self.feeder_id,
                    src_device=DEVICE_SCADA_MASTER,
                    dst_device=rtu_of(self.feeder_id),
                    message_type=MESSAGE_TYPE_POLL,
                    direction=DIRECTION_SCADA_TO_RTU,
                )
            ]
            for source in ("bus", "current", "summary"):
                builder = {
                    "bus": self._bus_events,
                    "current": self._current_events,
                    "summary": self._summary_events,
                }[source]
                events.extend(builder(grouped.get(source, [])))
            events.sort(key=lambda e: e.sort_key())
            yield timestamp, events

    def generate(self, output_path: Optional[Path] = None,
                 recorder: Optional["NetworkEventRecorder"] = None) -> "NetworkGenerationResult":
        """Write the network-event dataset, streaming timestamp by timestamp."""
        output_path = output_path or (self.results_dir / f"normal_{self.feeder_id}_network_events.csv")
        own = recorder is None
        if recorder is None:
            recorder = NetworkEventRecorder(output_path, scenario_id=self.scenario_id)
        result = NetworkGenerationResult(
            output_path=Path(output_path), scenario_id=self.scenario_id, feeder_id=self.feeder_id
        )
        recorder.open()
        try:
            for timestamp, events in self.iter_timestamp_groups():
                recorder.write(events)
                result.tracks(timestamp, events)
        finally:
            recorder.close()
        return result


class NetworkEventRecorder:
    """Validate, deterministically order, identity-assign and write events."""

    def __init__(self, output_path: Path, scenario_id: str = "") -> None:
        self.output_path = Path(output_path)
        self.scenario_id = scenario_id
        self._fh = None
        self._writer = None
        self._sequence = 0
        self._last_key: Optional[Tuple] = None

    def open(self) -> "NetworkEventRecorder":
        if self._fh is not None:
            return self
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.output_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=list(NETWORK_EVENT_COLUMNS))
        self._writer.writeheader()
        return self

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def __enter__(self) -> "NetworkEventRecorder":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def write(self, events: Iterable[NetworkEvent]) -> None:
        """Validate, sort and persist one batch of events."""
        if self._fh is None:
            self.open()
        batch = list(events)
        for event in batch:
            validate_normal_event(event)
        batch.sort(key=lambda e: e.sort_key())
        for event in batch:
            key = event.sort_key()
            if self._last_key is not None and key < self._last_key:
                raise NetworkEventError(
                    f"events out of deterministic order: {key} after {self._last_key}"
                )
            self._sequence += 1
            event_id = self.scenario_id or event.scenario_id
            identified = event.with_identity(
                event_id=f"{event_id}-ev{self._sequence:08d}", sequence=self._sequence
            )
            self._writer.writerow(identified.as_dict())
            self._last_key = key

    @property
    def sequence(self) -> int:
        return self._sequence


@dataclass
class NetworkGenerationResult:
    output_path: Path
    scenario_id: str
    feeder_id: str
    timestamps: List[str] = field(default_factory=list)
    #: counts summed over all events written.
    polls: int = 0
    telemetry: int = 0
    bus_points: int = 0
    current_points: int = 0
    summary_points: int = 0

    def tracks(self, timestamp: str, events: List[NetworkEvent]) -> None:
        self.timestamps.append(timestamp)
        for event in events:
            if event.message_type == MESSAGE_TYPE_POLL:
                self.polls += 1
            else:
                self.telemetry += 1
                if event.point_id.startswith("BUS_"):
                    self.bus_points += 1
                elif event.point_id.startswith("CURRENT_"):
                    self.current_points += 1
                else:
                    self.summary_points += 1

    @property
    def total_events(self) -> int:
        return self.polls + self.telemetry

    def as_report(self) -> List[str]:
        return [
            f"network events: {self.output_path}",
            f"scenario_id: {self.scenario_id}",
            f"timestamps: {len(self.timestamps)} ({self.timestamps[0] if self.timestamps else '-'} .. {self.timestamps[-1] if self.timestamps else '-'})",
            f"events: {self.total_events} (polls={self.polls}, telemetry={self.telemetry})",
            f"  bus points={self.bus_points}, current points={self.current_points}, summary points={self.summary_points}",
        ]


def read_network_events(path: Path) -> List[Dict[str, str]]:
    """Read a network-event dataset back as rows (validation/analysis helper)."""
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m simulator.network.normal",
        description="Generate the normal network-event dataset for one feeder.",
    )
    parser.add_argument("--feeder", required=True, help="feeder id, e.g. ieee37")
    parser.add_argument(
        "--results-dir",
        required=True,
        help="results directory holding normal_<feeder>_*telemetry*.csv + manifest",
    )
    parser.add_argument("--out", help="output CSV path (default: <results-dir>/normal_<feeder>_network_events.csv)")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"error: results directory not found: {results_dir}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    try:
        generator = NormalNetworkGenerator(results_dir, args.feeder)
        result = generator.generate(output_path=out)
    except NetworkEventError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(result.as_report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())