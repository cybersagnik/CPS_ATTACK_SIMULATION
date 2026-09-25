"""Initial MITRE ATT&CK for ICS attack scenarios.

Each scenario is a proper :class:`~simulator.attack.base.Attack` implementation
that shares the common engine -- never a stand-alone script.  Scenario ids
follow the Phase E convention: ``<attack_id>_<feeder_id>`` (e.g.
``unauthorized_command_ieee37``), so attack and normal rows are joined by the
same feeder-scoped id that an exporter will later key on.
"""

from __future__ import annotations

from .communication_disruption import CommunicationDisruptionAttack
from .false_measurement import FalseMeasurementAttack
from .multi_step_attack import MultiStepAttack
from .parameter_modification import ParameterModificationAttack
from .reconnaissance import ReconnaissanceAttack
from .unauthorized_command import UnauthorizedCommandAttack

__all__ = [
    "ReconnaissanceAttack",
    "UnauthorizedCommandAttack",
    "ParameterModificationAttack",
    "FalseMeasurementAttack",
    "CommunicationDisruptionAttack",
    "MultiStepAttack",
    "REGISTERED_SCENARIOS",
    "scenario_id_for",
]


def scenario_id_for(attack_id: str, feeder_id: str) -> str:
    """Deterministic scenario id, consistent with ``normal_<feeder>`` style."""
    return f"{attack_id}_{feeder_id}"


#: every scenario, keyed by attack id -- consumed by the engine's SCENARIOS.
REGISTERED_SCENARIOS = {
    "reconnaissance": ReconnaissanceAttack(),
    "unauthorized_command": UnauthorizedCommandAttack(),
    "parameter_modification": ParameterModificationAttack(),
    "false_measurement": FalseMeasurementAttack(),
    "communication_disruption": CommunicationDisruptionAttack(),
    "multi_step_attack": MultiStepAttack(),
}