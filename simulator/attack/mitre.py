"""MITRE ATT&CK for ICS technique catalog for the attack engine.

Every simulated attack scenario carries a machine-readable reference to the
official MITRE ATT&CK for ICS taxonomy (``https://attack.mitre.org``).  This
module is the single source of truth for the techniques the engine knows about.

Technique identifiers below were verified against the official source:

* ``T0846`` Remote System Discovery   (Discovery, TA0102)
* ``T0855`` Unauthorized Command Message (Impair Process Control, TA0106)
* ``T0836`` Modify Parameter          (Impair Process Control, TA0106)

Do **not** invent technique IDs here; only add techniques that exist in the
published ATT&CK for ICS matrix and record why a simulated behaviour maps to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _fields
from typing import Dict, Tuple

__all__ = [
    "MITREMapping",
    "MITRE_ICS",
    "TECHNIQUES",
    "mitre_for",
]

TACTIC_DISCOVERY = "Discovery"
TACTIC_IMPAIR_PROCESS_CONTROL = "Impair Process Control"


@dataclass(frozen=True)
class MITREMapping:
    """One machine-readable MITRE ATT&CK for ICS technique reference.

    ``rationale`` explains, in plain words, why the simulated behaviour of a
    scenario corresponds to the referenced technique -- kept in the data so no
    mapping ever floats around undocumented.
    """

    technique_id: str
    technique_name: str
    tactic: str
    rationale: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "technique_id": self.technique_id,
            "technique_name": self.technique_name,
            "tactic": self.tactic,
            "rationale": self.rationale,
        }


#: Verified catalog entries keyed by technique id.
TECHNIQUES: Dict[str, MITREMapping] = {
    "T0846": MITREMapping(
        technique_id="T0846",
        technique_name="Remote System Discovery",
        tactic=TACTIC_DISCOVERY,
        rationale=(
            "The scenario enumerates the logical assets, telemetry points and "
            "SCADA/RTU endpoints available in the simulated CPS environment, "
            "mirroring an adversary discovering operational assets prior to "
            "lateral movement or an attack."
        ),
    ),
    "T0855": MITREMapping(
        technique_id="T0855",
        technique_name="Unauthorized Command Message",
        tactic=TACTIC_IMPAIR_PROCESS_CONTROL,
        rationale=(
            "The scenario sends a simulated control command to a field asset "
            "without the logical precondition or authorisation for it, "
            "instructing the simulated device to perform an action outside "
            "normal bounds."
        ),
    ),
    "T0836": MITREMapping(
        technique_id="T0836",
        technique_name="Modify Parameter",
        tactic=TACTIC_IMPAIR_PROCESS_CONTROL,
        rationale=(
            "The scenario modifies a tunable parameter of a discovered "
            "controllable asset (e.g. a load multiplier) to a value outside "
            "the normal operating envelope and records the physical effect."
        ),
    ),
}


def mitre_for(technique_id: str) -> MITREMapping:
    """Look up a technique from the verified catalog."""
    try:
        return TECHNIQUES[technique_id]
    except KeyError:
        raise KeyError(
            f"unknown MITRE technique {technique_id!r}; add it to the "
            "verified catalog in simulator/attack/mitre.py"
        ) from None


def _repr(instance: MITREMapping) -> str:
    name = f"{instance.technique_id} {instance.technique_name}"
    if instance.technique_id in TECHNIQUES:
        name += " [verified]"
    return f"MITREMapping({name})"


# Keep frozen-dataclass repr readable; dataclass already gives a fine repr, so
# no override needed -- this attribute merely documents the available fields.
MITRE_FIELDS = [f.name for f in _fields(MITREMapping)]

#: Convenience container mirroring the README's MITRE taxonomy concept.
#: A scenario may attach one or more of these; the one decorated per scenario
#: in ``scenarios/`` is the primary technique, extras are recorded as related.
MITRE_ICS: Tuple[MITREMapping, ...] = tuple(
    TECHNIQUES[key] for key in ("T0846", "T0855", "T0836")
)