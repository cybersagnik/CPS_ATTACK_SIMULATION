"""Registry loader for the read-only feeder library.

Reads ``feeders/registry.yaml`` (single source of truth) and produces
:class:`FeederSource` objects.  Paths in the YAML historically pointed at a
``models/feeders/<id>`` layout; the actual library lives under ``feeders/<id>``.
The resolver therefore keys on the registry id and joins it against the real
library root so adapters always receive a working directory.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIBRARY_ROOT = Path(os.environ.get("CPS_FEEDERS_DIR", str(REPO_ROOT / "feeders")))
DEFAULT_REGISTRY = DEFAULT_LIBRARY_ROOT / "registry.yaml"


@dataclass(frozen=True)
class FeederSource:
    """Location + provenance of one downloadable feeder model."""

    id: str
    name: str
    status: str  # native_dss | csv_model | spec_data
    dir: Path
    source_url: str
    dss_master: Optional[Path] = None
    topology_file: Optional[Path] = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""


class FeederRegistry:
    """Loaded ``registry.yaml`` with id -> :class:`FeederSource` lookup."""

    def __init__(
        self,
        library_root: Path | str = DEFAULT_LIBRARY_ROOT,
        registry_path: Path | str | None = None,
    ) -> None:
        self.library_root = Path(library_root)
        self.registry_path = Path(registry_path) if registry_path else (
            self.library_root / "registry.yaml"
        )
        self._sources: Dict[str, FeederSource] = self._load()

    # -- loading ----------------------------------------------------------- #
    def _load(self) -> Dict[str, FeederSource]:
        if not self.registry_path.is_file():
            raise FileNotFoundError(
                f"Feeder registry not found at {self.registry_path}"
            )
        raw = yaml.safe_load(self.registry_path.read_text(encoding="utf-8"))
        entries: Dict[str, FeederSource] = {}
        for fid, entry in (raw.get("feeders") or {}).items():
            entries[fid] = self._build_source(fid, entry)
        return entries

    def _build_source(self, fid: str, entry: Mapping[str, Any]) -> FeederSource:
        dir_candidate = self._resolve_dir(fid, entry.get("dir"))
        dss_master = self._resolve_file(dir_candidate, entry.get("dss_master"))
        topology = self._resolve_file(dir_candidate, entry.get("topology_file"))
        return FeederSource(
            id=fid,
            name=str(entry.get("name", fid)),
            status=str(entry.get("status", "spec_data")),
            dir=dir_candidate,
            source_url=str(entry.get("source_url", "")),
            dss_master=dss_master,
            topology_file=topology,
            facts=dict(entry.get("facts") or {}),
            notes=str(entry.get("notes", "")),
        )

    def _resolve_dir(self, fid: str, declared: Any) -> Path:
        candidates: List[Path] = []
        if declared:
            candidates.append(Path(str(declared)))
        candidates.append(self.library_root / fid)
        for cand in candidates:
            if cand.is_dir():
                return cand
        m = list(self.library_root.rglob(fid))
        if len(m) == 1 and m[0].is_dir():
            return m[0]
        warnings.warn(
            f"Feeder directory for {fid!r} not found (declared {declared!r}); "
            f"using declared path {candidates[0]}",
            stacklevel=2,
        )
        return candidates[0]

    def _resolve_file(self, dir_path: Path, declared: Any) -> Optional[Path]:
        if not declared:
            return None
        cand = Path(str(declared))
        if cand.is_file():
            return cand
        joined = dir_path / cand.name
        return joined if joined.is_file() else None

    # -- access ------------------------------------------------------------ #
    def registered_ids(self) -> List[str]:
        return sorted(self._sources)

    def get(self, feeder_id: str) -> FeederSource:
        try:
            return self._sources[feeder_id]
        except KeyError:
            raise KeyError(
                f"Unknown feeder {feeder_id!r}; registered: {self.registered_ids()}"
            ) from None

    def sources(self) -> Mapping[str, FeederSource]:
        return dict(self._sources)

    def by_status(self, status: str) -> List[FeederSource]:
        return [s for s in self._sources.values() if s.status == status]


def load_source(feeder_id: str, source: FeederSource | None = None) -> FeederSource:
    """Compatibility helper: registry lookup for a feeder id."""
    return source if source is not None else FeederRegistry().get(feeder_id)