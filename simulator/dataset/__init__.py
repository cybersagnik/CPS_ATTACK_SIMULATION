"""Task H *dataset package*: deterministic, combined, attack-labeled dataset.

Public entry points (fail-closed, deterministic, canonical):
``generate_feeder_dataset``  end-to-end generation for one feeder
``export_feeder_artifacts``  deterministic artifact export (CSV/JSON/manifest)
``validate_combined``        strict canonical validation of a combined dataset
"""

from .errors import (
    DatasetError,
    DatasetGenerationError,
    DatasetValidationError,
    DatasetExportError,
    DatasetManifestError,
)
from .exporter import (
    ExportStats,
    export_feeder_artifacts,
)
from .manifest import ManifestEntry
from .schema import (
    DATASET_COLUMNS,
    MITRE_TECHNIQUE_COLUMN,
    ATTACK_ID_COLUMN,
    LABEL_COLUMN,
    LABEL_NORMAL,
    LABEL_ATTACK,
)
from .generator import FeederDataset, generate_feeder_dataset

__all__ = [
    "DatasetError",
    "DatasetGenerationError",
    "DatasetValidationError",
    "DatasetExportError",
    "DatasetManifestError",
    "ExportStats",
    "export_feeder_artifacts",
    "ManifestEntry",
    "DATASET_COLUMNS",
    "MITRE_TECHNIQUE_COLUMN",
    "ATTACK_ID_COLUMN",
    "LABEL_COLUMN",
    "LABEL_NORMAL",
    "LABEL_ATTACK",
    "FeederDataset",
    "generate_feeder_dataset",
]