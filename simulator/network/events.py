"""Network-event model for the logical SCADA / field-device layer (Phase E).

Protocol-neutral, deterministic representation of the SCADA dialogs used to
collect a feeder's *normal* telemetry.  ``protocol`` values (``DNP3``) are
logical labels only -- nothing here opens sockets, assembles protocol packets,
or models transport behaviour.  A normal network event carries one of two
message types:

* ``POLL``      -- SCADA_MASTER asks RTU_<feeder> for data (no payload).
* ``TELEMETRY`` -- RTU_<feeder> answers with one physical measurement point.

Every field is scalar/deterministic and defined below; the recorder assigns the
monotonic ``sequence`` and a deterministic ``event_id`` in output order so that
event identity is a pure function of the physical telemetry, not of wall-clock
time or filesystem enumeration.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Dict, Optional, Tuple

PROTOCOL_DNP3 = "DNP3"

MESSAGE_TYPE_POLL = "POLL"
MESSAGE_TYPE_TELEMETRY = "TELEMETRY"
MESSAGE_TYPES = (MESSAGE_TYPE_POLL, MESSAGE_TYPE_TELEMETRY)

DIRECTION_SCADA_TO_RTU = "SCADA_TO_RTU"
DIRECTION_RTU_TO_SCADA = "RTU_TO_SCADA"
DIRECTIONS = (DIRECTION_SCADA_TO_RTU, DIRECTION_RTU_TO_SCADA)

DEVICE_SCADA_MASTER = "SCADA_MASTER"

#: normal-phase quality/delivery/latency (attack phase widens these later).
QUALITY_GOOD = "GOOD"
DELIVERY_DELIVERED = "DELIVERED"
LATENCY_NORMAL_MS = 0

#: canonical CSV column order for the network-event dataset.
NETWORK_EVENT_COLUMNS = (
    "event_id",
    "scenario_id",
    "timestamp",
    "feeder_id",
    "src_device",
    "dst_device",
    "protocol",
    "message_type",
    "direction",
    "sequence",
    "point_id",
    "value",
    "unit",
    "quality",
    "delivery_status",
    "latency_ms",
)


class NetworkEventError(ValueError):
    """Raised for invalid network-event data or malformed Phase E inputs."""


def rtu_of(feeder_id: str) -> str:
    """Logical RTU endpoint name for a feeder, e.g. ``RTU_ieee37``."""
    return f"RTU_{feeder_id}"


def _message_type_rank(message_type: str) -> int:
    if message_type == MESSAGE_TYPE_POLL:
        return 0
    if message_type == MESSAGE_TYPE_TELEMETRY:
        return 1
    raise NetworkEventError(f"unknown message_type {message_type!r}")


def _device_roles(
    message_type: str, direction: str, feeder_id: str
) -> Tuple[str, str]:
    """Return the (src, dst) device pair required for the given message."""
    slave = rtu_of(feeder_id)
    if message_type == MESSAGE_TYPE_POLL:
        if direction != DIRECTION_SCADA_TO_RTU:
            raise NetworkEventError(
                f"POLL direction must be {DIRECTION_SCADA_TO_RTU}, got {direction!r}"
            )
        return (DEVICE_SCADA_MASTER, slave)
    if message_type == MESSAGE_TYPE_TELEMETRY:
        if direction != DIRECTION_RTU_TO_SCADA:
            raise NetworkEventError(
                f"TELEMETRY direction must be {DIRECTION_RTU_TO_SCADA}, got {direction!r}"
            )
        return (slave, DEVICE_SCADA_MASTER)
    raise NetworkEventError(f"unknown message_type {message_type!r}")


def _as_number(value: object, point_id: str) -> float:
    if not isinstance(value, (int, float)):
        raise NetworkEventError(f"value for {point_id!r} must be numeric, got {value!r}")
    number = float(value)
    if not isfinite(number):
        raise NetworkEventError(f"value for {point_id!r} must be finite, got {value!r}")
    return number


def validate_normal_event(event: "NetworkEvent") -> None:
    """Validate one event against the Phase E normal-scenario rules."""
    if not event.scenario_id:
        raise NetworkEventError("scenario_id must be non-empty")
    if not event.feeder_id:
        raise NetworkEventError("feeder_id must be non-empty")
    if not event.timestamp:
        raise NetworkEventError("timestamp must be non-empty")

    if event.protocol != PROTOCOL_DNP3:
        raise NetworkEventError(
            f"normal events use protocol {PROTOCOL_DNP3!r}, got {event.protocol!r}"
        )
    if event.quality != QUALITY_GOOD:
        raise NetworkEventError(f"normal quality must be {QUALITY_GOOD!r}, got {event.quality!r}")
    if event.delivery_status != DELIVERY_DELIVERED:
        raise NetworkEventError(
            f"normal delivery_status must be {DELIVERY_DELIVERED!r}, "
            f"got {event.delivery_status!r}"
        )
    if event.latency_ms != LATENCY_NORMAL_MS:
        raise NetworkEventError(
            f"normal latency must be {LATENCY_NORMAL_MS}, got {event.latency_ms!r}"
        )

    expected_src, expected_dst = _device_roles(
        event.message_type, event.direction, event.feeder_id
    )
    if event.src_device != expected_src:
        raise NetworkEventError(
            f"{event.message_type} src_device must be {expected_src!r}, got {event.src_device!r}"
        )
    if event.dst_device != expected_dst:
        raise NetworkEventError(
            f"{event.message_type} dst_device must be {expected_dst!r}, got {event.dst_device!r}"
        )

    if event.sequence is not None and (
        not isinstance(event.sequence, int) or event.sequence < 1
    ):
        raise NetworkEventError(f"sequence must be a positive int, got {event.sequence!r}")

    if event.message_type == MESSAGE_TYPE_POLL:
        if event.point_id or event.value is not None or event.unit:
            raise NetworkEventError(
                "POLL events carry no payload (point_id/value/unit must be empty)"
            )
    elif event.message_type == MESSAGE_TYPE_TELEMETRY:
        if not event.point_id:
            raise NetworkEventError("TELEMETRY events require a non-empty point_id")
        _as_number(event.value, event.point_id)


@dataclass(frozen=True)
class NetworkEvent:
    """One logical network event (POLL or TELEMETRY)."""

    event_id: Optional[str] = None
    scenario_id: str = ""
    timestamp: str = ""
    feeder_id: str = ""
    src_device: str = ""
    dst_device: str = ""
    protocol: str = PROTOCOL_DNP3
    message_type: str = MESSAGE_TYPE_TELEMETRY
    direction: str = DIRECTION_RTU_TO_SCADA
    sequence: Optional[int] = None
    point_id: str = ""
    value: Optional[float] = None
    unit: str = ""
    quality: str = QUALITY_GOOD
    delivery_status: str = DELIVERY_DELIVERED
    latency_ms: int = LATENCY_NORMAL_MS

    def validate(self) -> "NetworkEvent":
        """Validate and return this event (raises :class:`NetworkEventError`)."""
        validate_normal_event(self)
        return self

    def with_identity(self, event_id: str, sequence: int) -> "NetworkEvent":
        """Return a copy carrying the recorder-assigned identity."""
        return NetworkEvent(
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
        )

    def as_dict(self) -> Dict[str, str]:
        """Row dict ordered exactly as :data:`NETWORK_EVENT_COLUMNS`."""
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
        }

    def sort_key(self) -> Tuple[float, int, str, str, str]:
        """Deterministic ordering: timestamp -> message type -> src/dst -> point_id.

        Timestamps sort lexicographically because all telemetry timestamps are
        zero-padded ISO-8601 ``YYYY-MM-DDThh:mm:ss`` strings (naive local time),
        so string sort equals chronological sort without a timezone conversion.
        """
        return (
            self.timestamp,
            _message_type_rank(self.message_type),
            self.src_device,
            self.dst_device,
            self.point_id,
        )