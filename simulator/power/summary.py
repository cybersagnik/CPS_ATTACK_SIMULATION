"""Human-readable summary of loaded feeders (verification aid).

``python -m simulator.power.summary`` prints a table of every supported feeder
loaded through the common loader/adapter stack, e.g.::

    Feeder   | KV  | Nodes | Lines | Loads | ...
    IEEE37   | 4.8 | 37    | 35    | 25    | ...

Counts come from the live models so the table always reflects actual data.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .common_model import FeederModel
from .loader import FeederLoader

#: (row key, human label) -- order defines the column order of the table.
COLUMNS: List[tuple] = [
    ("feeder", "Feeder"),
    ("nominal_kv", "KV"),
    ("nodes", "Nodes"),
    ("lines", "Lines"),
    ("loads", "Loads"),
    ("generators", "Generators"),
    ("transformers", "Transformers"),
    ("regulators", "Regulators"),
    ("switches", "Switches"),
    ("capacitors", "Capacitors"),
    ("line_configs", "Line Configs"),
    ("measurements", "Measurements"),
]


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def summarize_feeder(model: FeederModel) -> Dict[str, object]:
    """One row of the summary for a single loader-produced model."""
    counts = model.device_counts()
    return {
        "feeder": model.id,
        "nominal_kv": model.nominal_kv,
        "nodes": counts["buses"],
        "lines": counts["lines"],
        "loads": counts["loads"],
        "generators": counts["generators"],
        "transformers": counts["transformers"],
        "regulators": counts["regulators"],
        "switches": counts["switches"],
        "capacitors": counts["capacitors"],
        "line_configs": len(model.configurations),
        "measurements": counts["measurements"],
    }


def render_table(rows: List[Dict[str, object]]) -> List[str]:
    """Render summary rows as a left-aligned ``|``-separated table."""
    keys = [key for key, _ in COLUMNS]
    headers = [label for _, label in COLUMNS]
    data = [[_fmt(row.get(key, "")) for key in keys] for row in rows]
    widths = [
        max(
            len(headers[i]),
            *(len(row[i]) for row in data),
        )
        for i in range(len(headers))
    ]
    lines = [
        "|".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "-+-".join("-" * widths[i] for i in range(len(headers))),
    ]
    lines += [
        "|".join(row[i].ljust(widths[i]) for i in range(len(headers)))
        for row in data
    ]
    return lines


def build_summary(
    loader: Optional[FeederLoader] = None,
    feeder_ids: Optional[List[str]] = None,
) -> List[Dict[str, object]]:
    """Load every feeder with a real adapter manifest and summarize it."""
    loader = loader or FeederLoader()
    if feeder_ids is None:
        feeder_ids = [
            fid
            for adapter in loader.adapters
            for fid in adapter.supported_ids()
            if adapter.accepts(loader.registry.get(fid).status)
        ]
    return [summarize_feeder(loader.load(fid)) for fid in feeder_ids]


def main() -> None:
    loader = FeederLoader()
    print("\n".join(render_table(build_summary(loader))))


if __name__ == "__main__":
    main()