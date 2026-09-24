"""Attack-phase network-event model (extends the Phase E normal baseline).

Phase E defines the *normal* event model in ``simulator.network.events``:
``POLL`` / ``TELEMETRY`` dialogs with ``quality=GOOD``,
``delivery_status=DELIVERED`` and ``latency_ms=0``.  The attack phase never
rewrites those semantics; it *extends* the schema with attack-specific message
types and attack-specific fields (the ones the network workflow already listed
as planned: ``injected``, ``replay_of_event_id``, ``original_value``,
``reported_value``, ``command_id``, ``result``).

New logical message types (still protocol-neutral, ``protocol="DNP3"`` is a
logical label; no sockets, no packets, no real DNP3):

* ``QUERY``   -- SCADA_MASTER -> RTU_<feeder>; attacker-driven interrogation
                 (no payload), used by reconnaissance.
* ``COMMAND`` -- SCADA_MASTER -> RTU_<feeder>; a simulated control command to a
                 discovered control point, carrying ``command_id``, the
                 targeted ``point_id`` and the commanded ``value``.
* ``REPORT``  -- RTU_<feeder> -> SCADA_MASTER; the simulated device response:
                 a point read-back carrying the ``reported_value``, or the
                 acknowledgement of a command.

Non-normal ``quality`` / ``delivery_status`` / ``latency_ms`` values are allowed
*only* where a specific attack scenario requires them; most attack events here
stay GOOD/DELIVERED/0 because the *data transport itself* is fine -- it is the
intent (or an injected value) that makes the traffic malicious.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Dict, Optional, Tuple

from simulator.network.events import (
    DEVICE_SCADA_MASTER,
    DIRECTION_RTU_TO_SCADA,
    DIRECTION_SCADA_TO_RTU,
    NETWORK_EVENT_COLUMNS,
    PROTOCOL_DNP3,
    rtu_of,
)

MESSAGE_TYPE_QUERY = "QUERY"
MESSAGE_TYPE_COMMAND = "COMMAND"
MESSAGE_TYPE_REPORT = "REPORT"
ATTACK_MESSAGE_TYPES = (MESSAGE_TYPE_QUERY, MESSAGE_TYPE_COMMAND, MESSAGE_TYPE_REPORT)

#: The Phase E normal schema plus the planned attack-phase extension fields.
ATTACK_EVENT_COLUMNS = NETWORK_EVENT_COLUMNS + (
    "injected",
    "replay_of_event_id",
    "original_value",
    "reported_value",
    "command_id",
    "result",
)


class AttackEventError(ValueError):
    """Raised for invalid attack-phase network-event data."""


def _rank(message_type: str) -> int:
    if message_type == "POLL":
        return 0
    if message_type == "QUERY":
        return 1
    if message_type == "COMMAND":
        return 2
    if message_type == "REPORT":
        return 3
    if message_type == "TELEMETRY":
        return 4
    raise AttackEventError(f"unknown message_type {message_type!r}")


def _numeric(value: object, where: str) -> float:
    if not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise AttackEventError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def validate_attack_event(event: "AttackEvent") -> None:
    """Validate one event against the Phase F/G event-layer rules.

    Normal-shaped events (``POLL`` / ``TELEMETRY``) keep their Phase E shape
    (including the normal quality/delivery/latency constants).  Attack message
    types follow the attack-layer shapes below.  ``protocol`` stays ``DNP3``
    (logical label) for every event in this layer.
    """
    if not event.scenario_id:
        raise AttackEventError("scenario_id must be non-empty")
    if not event.feeder_id:
        raise AttackEventError("feeder_id must be non-empty")
    if not event.timestamp:
        raise AttackEventError("timestamp must be non-empty")
    if event.protocol != PROTOCOL_DNP3:
        raise AttackEventError(f"events use protocol {PROTOCOL_DNP3!r}, got {event.protocol!r}")

    rtu = rtu_of(event.feeder_id)
    if event.message_type == "POLL":
        if event.direction != DIRECTION_SCADA_TO_RTU or event.src_device != DEVICE_SCADA_MASTER:
            raise AttackEventError(f"POLL must be {DEVICE_SCADA_MASTER} -> {rtu}")
        if event.dst_device != rtu:
            raise AttackEventError(f"POLL dst must be {rtu}")
        if event.point_id or event.value is not None or event.unit:
            raise AttackEventError("POLL events carry no payload")
    elif event.message_type == "TELEMETRY":
        if event.direction != DIRECTION_RTU_TO_SCADA or event.src_device != rtu:
            raise AttackEventError(f"TELEMETRY must be {rtu} -> {DEVICE_SCADA_MASTER}")
        if event.dst_device != DEVICE_SCADA_MASTER:
            raise AttackEventError(f"TELEMETRY dst must be {DEVICE_SCADA_MASTER}")
        if not event.point_id:
            raise AttackEventError("TELEMETRY events require a non-empty point_id")
        _numeric(event.value, "TELEMETRY value")
    elif event.message_type == MESSAGE_TYPE_QUERY:
        if event.direction != DIRECTION_SCADA_TO_RTU or event.src_device != DEVICE_SCADA_MASTER:
            raise AttackEventError(f"QUERY must be {DEVICE_SCADA_MASTER} -> {rtu}")
        if event.dst_device != rtu:
            raise AttackEventError(f"QUERY dst must be {rtu}")
        if event.point_id or event.value is not None or event.unit:
            raise AttackEventError("QUERY events carry no payload")
        if not event.command_id and not event.result:
            raise AttackEventError("QUERY events require a command_id or result")
    elif event.message_type == MESSAGE_TYPE_COMMAND:
        if event.direction != DIRECTION_SCADA_TO_RTU or event.src_device != DEVICE_SCADA_MASTER:
            raise AttackEventError(f"COMMAND must be {DEVICE_SCADA_MASTER} -> {rtu}")
        if event.dst_device != rtu:
            raise AttackEventError(f"COMMAND dst must be {rtu}")
        if not event.point_id:
            raise AttackEventError("COMMAND events require a non-empty point_id")
        _numeric(event.value, "COMMAND value")
        if not event.command_id:
            raise AttackEventError("COMMAND events require a command_id")
    elif event.message_type == MESSAGE_TYPE_REPORT:
        if event.direction != DIRECTION_RTU_TO_SCADA or event.src_device != rtu:
            raise AttackEventError(f"REPORT must be {rtu} -> {DEVICE_SCADA_MASTER}")
        if event.dst_device != DEVICE_SCADA_MASTER:
            raise AttackEventError(f"REPORT dst must be {DEVICE_SCADA_MASTER}")
        if not event.point_id:
            raise AttackEventError("REPORT events require a non-empty point_id")
        _numeric(event.value, "REPORT value")
    else:
        raise AttackEventError(f"unknown message_type {event.message_type!r}")

    if event.sequence is not None and (not isinstance(event.sequence, int) or event.sequence < 1):
        raise AttackEventError(f"sequence must be a positive int, got {event.sequence!r}")


@dataclass(frozen=True)
class AttackEvent:
    """One attack-phase network event: the Phase E schema plus attack fields.

    ``injected`` flags messages that did not originate from the Phase E normal
    dialog: attacker-forged ``COMMAND``/``QUERY`` messages (injected into the
    SCADA<=>RTU stream) and any ``REPORT`` carrying a value the operator cannot
    trust (data forgery, e.g. a tampered parameter reported as the operating
    value).  Genuine device echoes of a *true* new state (e.g. the RTU honestly
    acknowledging an applied unauthorized command) keep ``injected=False``.
    ``replay_of_event_id`` links a replayed event to the normal event it
    replays.  ``original_value`` and ``reported_value`` hold the pre-attack
    value and the value reported to the operator respectively.  ``result`` is a
    free status token (e.g. ``DISCOVERED``, ``APPLIED``, ``DENIED``).
    """

    event_id: Optional[str] = None
    scenario_id: str = ""
    timestamp: str = ""
    feeder_id: str = ""
    src_device: str = ""
    dst_device: str = ""
    protocol: str = PROTOCOL_DNP3
    message_type: str = MESSAGE_TYPE_REPORT
    direction: str = DIRECTION_RTU_TO_SCADA
    sequence: Optional[int] = None
    point_id: str = ""
    value: Optional[float] = None
    unit: str = ""
    quality: str = "GOOD"
    delivery_status: str = "DELIVERED"
    latency_ms: int = 0
    # -- attack extension -------------------------------------------------- #
    injected: bool = False
    replay_of_event_id: str = ""
    original_value: Optional[object] = None
    reported_value: Optional[object] = None
    command_id: str = ""
    result: str = ""

    def validate(self) -> "AttackEvent":
        validate_attack_event(self)
        return self

    def with_identity(self, event_id: str, sequence: int) -> "AttackEvent":
        return AttackEvent(
            event_id=event_id,
            scenario_id=self.scenario_id,
            timestamp=self.timestamp,
            feeder_id=self.feeder_id,
            src_device=self.src_device,
            dst_device=self.dst_device,
            protocol=self.protocol,
            message_type=self.message_type,
            direction=self.direction,
            sequence=sequence,
            point_id=self.point_id,
            value=self.value,
            unit=self.unit,
            quality=self.quality,
            delivery_status=self.delivery_status,
            latency_ms=self.latency_ms,
            injected=self.injected,
            replay_of_event_id=self.replay_of_event_id,
            original_value=self.original_value,
            reported_value=self.reported_value,
            command_id=self.command_id,
            result=self.result,
        )

    def as_dict(self) -> Dict[str, object]:
        """Row dict ordered exactly as :data:`ATTACK_EVENT_COLUMNS`."""
        return {
            "event_id": self.event_id or "",
            "scenario_id": self.scenario_id,
            "timestamp": self.timestamp,
            "feeder_id": self.feeder_id,
            "src_device": self.src_device,
            "dst_device": self.dst_device,
            "protocol": self.protocol,
            "message_type": self.message_type,
            "direction": self.direction,
            "sequence": f"{self.sequence}" if self.sequence is not None else "",
            "point_id": self.point_id,
            "value": "" if self.value is None else repr(self.value),
            "unit": self.unit,
            "quality": self.quality,
            "delivery_status": self.delivery_status,
            "latency_ms": f"{int(self.latency_ms)}",
            "injected": "1" if self.injected else "0",
            "replay_of_event_id": self.replay_of_event_id,
            "original_value": "" if self.original_value is None else repr(self.original_value),
            "reported_value": "" if self.reported_value is None else repr(self.reported_value),
            "command_id": self.command_id,
            "result": self.result,
        }

    def sort_key(self) -> Tuple[object, ...]:
        """Deterministic ordering compatible with the Phase E event ordering."""
        return (
            self.timestamp,
            _rank(self.message_type),
            self.src_device,
            self.dst_device,
            self.point_id,
        )


def event_from_dict(row: Dict[str, object]) -> AttackEvent:
    """Deserialise a row dict (as produced by :meth:`AttackEvent.as_dict`)."""
    return AttackEvent(
        event_id=row.get("event_id") or None,
        scenario_id=str(row.get("scenario_id") or ""),
        timestamp=str(row.get("timestamp") or ""),
        feeder_id=str(row.get("feeder_id") or ""),
        src_device=str(row.get("src_device") or ""),
        dst_device=str(row.get("dst_device") or ""),
        protocol=str(row.get("protocol") or PROTOCOL_DNP3),
        message_type=str(row.get("message_type") or ""),
        direction=str(row.get("direction") or ""),
        sequence=int(row["sequence"]) if str(row.get("sequence") or "").strip() else None,
        point_id=str(row.get("point_id") or ""),
        value=None if str(row.get("value") or "").strip() == "" else float(row["value"]),
        unit=str(row.get("unit") or ""),
        quality=str(row.get("quality") or "GOOD"),
        delivery_status=str(row.get("delivery_status") or "DELIVERED"),
        latency_ms=int(float(row.get("latency_ms") or 0)),
        injected=bool(int(row.get("injected") or 0)),
        replay_of_event_id=str(row.get("replay_of_event_id") or ""),
        original_value=None if str(row.get("original_value") or "").strip() == "" else row["original_value"],
        reported_value=None if str(row.get("reported_value") or "").strip() == "" else row["reported_value"],
        command_id=str(row.get("command_id") or ""),
        result=str(row.get("result") or ""),
    )