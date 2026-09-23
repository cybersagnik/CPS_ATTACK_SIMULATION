"""Orchestration: registry -> adapter -> common model.

The :class:`FeederLoader` is the only entry point the pipeline needs to turn a
feeder id into a :class:`~simulator.power.common_model.FeederModel`.
"""

from __future__ import annotations

from typing import List, Optional

from .adapters.base import FeederAdapter
from .adapters.xls_spec_adapter import KerstingXlsAdapter
from .common_model import FeederModel
from .feeder_registry import FeederRegistry, FeederSource

_UNIMPLEMENTED_HINT = {
    "native_dss": "an OpenDSS-native adapter (YYD) is not wired yet",
    "csv_model": "a CSV-model adapter (LVNTS / European LV / 8500-node) is not wired yet",
}


class FeederLoader:
    """Resolve a feeder id and load it into the common representation."""

    def __init__(self, registry: Optional[FeederRegistry] = None) -> None:
        self.registry = registry or FeederRegistry()
        self.adapters: List[FeederAdapter] = [KerstingXlsAdapter()]

    # -- loading ----------------------------------------------------------- #
    def load(self, feeder_id: str) -> FeederModel:
        source = self.registry.get(feeder_id)
        return self.load_source(source)

    def load_source(self, source: FeederSource) -> FeederModel:
        for adapter in self.adapters:
            if adapter.accepts(source.status):
                return adapter.load(source)
        hint = _UNIMPLEMENTED_HINT.get(source.status, f"status {source.status!r}")
        raise NotImplementedError(
            f"Feeder {source.id!r} ({source.status}) has no adapter yet: {hint}"
        )

    def supported_feeder_ids(self) -> List[str]:
        return [
            fid
            for fid in self.registry.registered_ids()
            if self._has_adapter(fid)
        ]

    def unsupported_feeder_ids(self) -> List[str]:
        return [
            fid
            for fid in self.registry.registered_ids()
            if not self._has_adapter(fid)
        ]

    def _has_adapter(self, feeder_id: str) -> bool:
        source = self.registry.get(feeder_id)
        return any(a.accepts(source.status) for a in self.adapters)