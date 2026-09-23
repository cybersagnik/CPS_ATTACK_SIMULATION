"""Adapter protocol: raw feeder model -> :class:`FeederModel`.

Every supported feeder format implements this interface.  The rest of the
pipeline only ever talks to a :class:`FeederModel`; it never touches raw
OpenDSS / XLS / CSV files, so new feeders only need a new adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Tuple

from ..common_model import FeederModel
from ..feeder_registry import FeederSource


class FeederAdapter(ABC):
    """Convert one feeder source into the common representation.

    ``status_kinds`` declares which registry statuses this adapter can handle
    (``native_dss``, ``csv_model``, ``spec_data``).
    """

    status_kinds: Tuple[str, ...] = ()

    def accepts(self, status: str) -> bool:
        return status in self.status_kinds

    @abstractmethod
    def load(self, source: FeederSource) -> FeederModel:
        """Load ``source`` and return a fully populated common model."""