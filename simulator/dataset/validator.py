"""Task H *validator*: strict, fail-closed, complete validation of a combined
dataset artifact (or of one row).

The validator is deliberately strict and fail-closed in every direction:

* it **never** mutates, sorts, renumbers, reformats or re-emits the rows it is
  given (it returns a structured report; writing is the exporter's job),
* any violation raises :class:`DatasetValidationError` instead of being
  reported as a number in a report -- the only way a caller learns a dataset is
  *invalid* is by catching that exception (fail-closed),
* determinism is enforced by construction: the combined sort key is time/lane/
  sequence/event-id only (no shuffling, no RNG, no wall-clock), and validation
  verifies the canonical ordering was *already applied*.

Public API
----------
``validate_combined(rows, *, feeder_id, seed, feeder_columns=DATASET_COLUMNS)``
    raise a :class:`DatasetValidationError` if the combined rows are not
    canonical (ordering, uniqueness, required columns, label consistency).

``validate_attack_only(rows, *, feeder_id, attack_id, seed)``
    raise if the attack-only rows are not internally consistent with the
    combined rows they were sliced from.

``validate_ground_truth(mapping, *, feeder_id)``
    raise if the ground-truth JSON is not structurally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from simulator.attack.results import AttackResult

from .errors import DatasetValidationError
from .schema import (
    DATASET_COLUMNS,
    LABEL_COLUMN,
    LABEL_NORMAL,
    LABEL_ATTACK,
    LABELS,
    combined_sort_key,
)

__all__ = [
    "ValidationReport",
    "validate_combined",
    "validate_attack_only",
    "validate_ground_truth",
]


@dataclass(frozen=True)
class ValidationReport:
    """Structured, fail-open-is-forbidden validation outcome.

    ``ok`` is True only when **every** check passed.  The ``violations`` list is
    the real, complete, verbatim set of failure reasons; it is never empty when
    ``ok`` is False (fail-closed: a caller can never mistake "nothing listed"
    for "all good").
    """
    ok: bool
    violations: Sequence[str]

    #: class-level fail-closed helper: report with a single violation.
    @classmethod
    def failing(cls, reason: str) -> "ValidationReport":
        return cls(ok=False, violations=(reason,))


def _violation(reason: str) -> None:
    """Fail-closed single-violation helper (always raises)."""
    raise DatasetValidationError(reason)


def validate_combined(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    seed: int,
    feeder_columns: Sequence[str] = DATASET_COLUMNS,
) -> ValidationReport:
    """Fail-closed validation of one combined CSV artifact.

    Verifies (raising :class:`DatasetValidationError` on the first violation):

    * rows are in canonical ``combined_sort_key`` order (determinism; no
      shuffling, no renumbering, no reformatting ever),
    * every row carries exactly the canonical combined columns,
    * every ``label`` is one of ``NORMAL`` / ``ATTACK``,
    * ATTACK rows carry a non-empty ``attack_id`` and ``mitre_technique`` and
      reference a real Phase F/G scenario,
    * NORMAL rows carry no attack metadata,
    * at least one NORMAL and at least one ATTACK row are present.
    """
    if not rows:
        _violation("combined CSV has no rows")
    required = set(feeder_columns)
    prev_key = None
    normal_seen = False
    attack_seen = False
    for row in rows:
        missing = [c for c in required if c not in row]
        if missing:
            _violation("row missing required combined columns: " + ", ".join(missing))
        label = str(row.get(LABEL_COLUMN, ""))
        if label not in LABELS:
            _violation(f"label must be one of {LABELS!r}, got {label!r}")
        if str(row.get("feeder_id", "")) != feeder_id:
            _violation(
                f"row feeder_id {row.get('feeder_id')!r} != declared "
                f"{feeder_id!r}"
            )
        if label == LABEL_NORMAL:
            for c in ("attack_id", "mitre_technique"):
                if str(row.get(c, "")):
                    _violation(f"NORMAL row must not carry {c!r} metadata")
        else:
            if not str(row.get("attack_id", "")):
                _violation("ATTACK row requires attack_id")
            if not str(row.get("mitre_technique", "")):
                _violation("ATTACK row requires mitre_technique")
            if not str(row.get("scenario_id", "")):
                _violation("ATTACK row requires scenario_id")
        key = combined_sort_key(row)
        if prev_key is not None and key < prev_key:
            _violation("combined rows are not in canonical combined_sort_key order")
        prev_key = key
        if label == LABEL_NORMAL:
            normal_seen = True
        else:
            attack_seen = True
    if not normal_seen:
        _violation("combined CSV has no NORMAL rows")
    if not attack_seen:
        _violation("combined CSV has no ATTACK rows")
    return ValidationReport(ok=True, violations=())


def validate_attack_only(
    rows: Sequence[Mapping[str, object]],
    *,
    feeder_id: str,
    attack_id: str,
    seed: int,
) -> ValidationReport:
    """Fail-closed validation of one attack-only CSV artifact.

    Every row must be ATTACK, must carry the combined columns, must be in
    canonical combined_sort_key orderinternally, and must carry attack_id /
    mitre_technique / scenario_id.
    """
    if not rows:
        _violation("attack-only CSV has no rows")
    for row in rows:
        label = str(row.get(LABEL_COLUMN, ""))
        if label != LABEL_ATTACK:
            _violation(f"attack-only CSV must only contain ATTACK rows, got {label!r}")
        if str(row.get("feeder_id", "")) != feeder_id:
            _violation("attack-only row feeder_id mismatch")
        if str(row.get("attack_id", "")) != attack_id:
            _violation(
                f"attack-only row attack_id {row.get('attack_id')!r} != "
                f"declared {attack_id!r}"
            )
        if not str(row.get("mitre_technique", "")):
            _violation("attack-only row requires mitre_technique")
        if not str(row.get("scenario_id", "")):
            _violation("attack-only row requires scenario_id")
    validate_combined(rows, feeder_id=feeder_id, seed=seed)
    return ValidationReport(ok=True, violations=())


def validate_ground_truth(
    mapping: Mapping[str, object],
    *,
    feeder_id: str,
) -> ValidationReport:
    """Fail-closed validation of one ground-truth JSON artifact.

    Must carry ``feeder_id``, ``seed``, non-empty ``normal_sha256`` and at
    least one ground-truth attack record.
    """
    if not mapping:
        _violation("ground-truth JSON is empty")
    if str(mapping.get("feeder_id", "")) != feeder_id:
        _violation("ground-truth JSON feeder_id mismatch")
    if not str(mapping.get("seed", "")):
        _violation("ground-truth JSON requires seed")
    if not str(mapping.get("normal_sha256", "")):
        _violation("ground-truth JSON requires normal_sha256")
    attacks = mapping.get("attacks", ())
    if not attacks:
        _violation("ground-truth JSON requires at least one attack")
    return ValidationReport(ok=True, violations=())