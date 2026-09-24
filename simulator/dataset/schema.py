"""Task H: combined 25-column dataset schema, labels and fail-closed validation.

Task H composes a single canonical **25-column** combined schema out of the two
canonical Phase schemas plus the Task H annotation layer.  It does **not**
re-declare, rename, renumber, reformat, or otherwise alter any Phase E or Phase
F/G column: it imports the canonical ``NETWORK_EVENT_COLUMNS`` (16) and
``ATTACK_EVENT_COLUMNS`` (22) tuples verbatim and appends the **3** Task H
columns (``label``, ``attack_id``, ``mitre_technique``).  Task H never looks at
DNP3 as anything beyond a logical protocol label.

The combined 25-column layout of one row:

1. the **16** Phase E ``NETWORK_EVENT_COLUMNS`` (telemetry, verbatim),
2. the **6** Phase F/G attack-extension columns
   (``injected``, ``replay_of_event_id``, ``original_value``,
   ``reported_value``, ``command_id``, ``result``) -- exactly
   ``ATTACK_EVENT_COLUMNS[len(NETWORK_EVENT_COLUMNS):]``,
3. the **3** Task H columns (``label``, ``attack_id``, ``mitre_technique``).

All builders/validators are fail-closed: any malformed row raises instead of
being silently accepted, so a broken combined artifact can never be emitted.
"""

from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

from simulator.attack.events import ATTACK_EVENT_COLUMNS
from simulator.network.events import NETWORK_EVENT_COLUMNS

from .errors import DatasetError, DatasetError

__all__ = [
    "TASK_H_COLUMNS",
    "LABEL_COLUMN",
    "ATTACK_ID_COLUMN",
    "MITRE_TECHNIQUE_COLUMN",
    "LABEL_NORMAL",
    "LABEL_ATTACK",
    "LABELS",
    "DATASET_COLUMNS",
    "normal_row",
    "attack_row",
    "combined_sort_key",
    "validate_row",
    "validate_combined",
]

# --------------------------------------------------------------------------- #
# canonical columns
# --------------------------------------------------------------------------- #
LABEL_COLUMN = "label"
ATTACK_ID_COLUMN = "attack_id"
MITRE_TECHNIQUE_COLUMN = "mitre_technique"
TASK_H_COLUMNS: Tuple[str, ...] = (
    LABEL_COLUMN,
    ATTACK_ID_COLUMN,
    MITRE_TECHNIQUE_COLUMN,
)

LABEL_NORMAL = "NORMAL"
LABEL_ATTACK = "ATTACK"
LABELS = (LABEL_NORMAL, LABEL_ATTACK)

#: canonical layer tie-break index: NORMAL sorts before ATTACK at an identical
#: timestamp.
_LAYER_INDEX = {LABEL_NORMAL: 0, LABEL_ATTACK: 1}

#: the full 25-column canonical combined schema.
DATASET_COLUMNS: Tuple[str, ...] = (
    *NETWORK_EVENT_COLUMNS,  # 16 Phase E
    *ATTACK_EVENT_COLUMNS[len(NETWORK_EVENT_COLUMNS):],  # 6 Phase F/G
    *TASK_H_COLUMNS,  # 3 Task H
)


# --------------------------------------------------------------------------- #
# row builders (verbatim, deterministic)
# --------------------------------------------------------------------------- #
def normal_row(
    event: Mapping[str, object],
    *,
    feeder_id: str,
) -> Dict[str, object]:
    """One combined NORMAL row from one Phase E network event.

    The 16 Phase E values are copied verbatim (no reformatting).  The 6 F/G and
    3 Task H columns are empty.  ``label`` is NORMAL.
    """
    row: Dict[str, object] = {c: "" for c in DATASET_COLUMNS}
    for c in NETWORK_EVENT_COLUMNS:
        row[c] = event.get(c, "")
    row[LABEL_COLUMN] = LABEL_NORMAL
    return row


def attack_row(
    event: Mapping[str, object],
    *,
    feeder_id: str,
    attack_id: str,
    mitre_technique: str,
) -> Dict[str, object]:
    """One combined ATTACK row from one Phase F/G attack event.

    The 22 Phase E + F/G values are copied verbatim (no reformatting).  The 3
    Task H columns carry the label and scenario metadata.
    """
    row: Dict[str, object] = {c: "" for c in DATASET_COLUMNS}
    for c in ATTACK_EVENT_COLUMNS:
        row[c] = event.get(c, "")
    row[LABEL_COLUMN] = LABEL_ATTACK
    row[ATTACK_ID_COLUMN] = attack_id
    row[MITRE_TECHNIQUE_COLUMN] = mitre_technique
    return row


# --------------------------------------------------------------------------- #
# deterministic ordering (no shuffle, ever)
# --------------------------------------------------------------------------- #
def combined_sort_key(row: Mapping[str, object]) -> Tuple[str, ...]:
    """Deterministic combined-row sort key.

    ``(timestamp ISO-8601, lane, sequence, event_id)``:

    * ``timestamp`` lexical == chronological (never reordered),
    * ``lane`` NORMAL(0) precedes ATTACK(1) at an identical timestamp,
    * ``sequence`` verbatim per-stream chronology (never renumbered),
    * ``event_id`` verbatim deterministic tie-break.

    No shuffling happens: each stream keeps its own Phase chronology; the lane
    tie-break only ever orders two rows that *share* a timestamp.
    """
    return (
        str(row.get("timestamp", "")),
        str(_LAYER_INDEX.get(str(row.get(LABEL_COLUMN, "")), 0)),
        str(row.get("sequence", "")),
        str(row.get("event_id", "")),
    )


# --------------------------------------------------------------------------- #
# fail-closed validation
# --------------------------------------------------------------------------- #
def validate_row(
    row: Mapping[str, object],
    *,
    feeder_id: str,
    expected_scenario_ids: Sequence[str],
    expected_attack_ids: Sequence[str],
) -> None:
    """Validate a single combined-dataset row, fail-closed.

    Raises :class:`DatasetError` on any violation: a row that is not
    structurally complete, or an ATTACK row that references an unknown
    scenario/attack id, or a NORMAL row that carries attack metadata.
    """
    missing = [c for c in DATASET_COLUMNS if c not in row]
    if missing:
        raise DatasetError(
            "row missing required combined columns: " + ", ".join(missing)
        )
    label = str(row.get(LABEL_COLUMN, ""))
    if label not in LABELS:
        raise DatasetError(
            f"label must be one of {LABELS!r}, got {label!r}"
        )
    if str(row.get("feeder_id", "")) != feeder_id:
        raise DatasetError(
            f"row feeder_id {row.get('feeder_id')!r} != declared {feeder_id!r}"
        )
    if label == LABEL_NORMAL:
        for c in (ATTACK_ID_COLUMN, MITRE_TECHNIQUE_COLUMN):
            if str(row.get(c, "")):
                raise DatasetError(f"NORMAL row must not carry {c!r} metadata")
    else:
        attack_id = str(row.get(ATTACK_ID_COLUMN, ""))
        if not attack_id:
            raise DatasetError("ATTACK row requires attack_id")
        if attack_id not in expected_attack_ids:
            raise DatasetError(
                f"ATTACK row attack_id {attack_id!r} not in expected set "
                f"{sorted(expected_attack_ids)!r}"
            )
        scenario_id = str(row.get("scenario_id", ""))
        if scenario_id not in expected_scenario_ids:
            raise DatasetError(
                f"ATTACK row scenario_id {scenario_id!r} not in expected set "
                f"{sorted(expected_scenario_ids)!r}"
            )
        if not str(row.get(MITRE_TECHNIQUE_COLUMN, "")):
            raise DatasetError("ATTACK row requires mitre_technique")


def validate_combined(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    expected_scenario_ids: Sequence[str],
    expected_attack_ids: Sequence[str],
) -> None:
    """Validate a full combined dataset, fail-closed.

    Also checks the rows are in canonical ``combined_sort_key`` order and the
    dataset contains at least one NORMAL and at least one ATTACK row.
    """
    if not rows:
        raise DatasetError("combined dataset is empty")
    prev_key = None
    normal_seen = False
    attack_seen = False
    for row in rows:
        validate_row(
            row,
            feeder_id=feeder_id,
            expected_scenario_ids=expected_scenario_ids,
            expected_attack_ids=expected_attack_ids,
        )
        key = combined_sort_key(row)
        if prev_key is not None and key < prev_key:
            raise DatasetError(
                "rows are not in canonical combined_sort_key order"
            )
        prev_key = key
        if str(row.get(LABEL_COLUMN, "")) == LABEL_NORMAL:
            normal_seen = True
        else:
            attack_seen = True
    if not normal_seen:
        raise DatasetError("dataset contains no NORMAL rows")
    if not attack_seen:
        raise DatasetError("dataset contains no ATTACK rows")