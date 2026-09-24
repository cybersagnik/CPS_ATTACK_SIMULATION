"""Task H exception types (fail-closed: every Task H failure raises)."""

from __future__ import annotations


class DatasetError(Exception):
    """Base class for all Task H dataset errors."""


class DatasetGenerationError(DatasetError):
    """Raised when a Task H dataset cannot be produced (fail-closed)."""


class DatasetValidationError(DatasetError):
    """Raised when a Task H dataset fails validation (fail-closed)."""


class DatasetExportError(DatasetError):
    """Raised when a Task H dataset artifact cannot be exported."""


class DatasetManifestError(DatasetError):
    """Raised when a Task H manifest cannot be produced or verified."""