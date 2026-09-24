#!/usr/bin/env python3
"""CLI: run the registered attack scenarios against a feeder (Phase F/G).

Usage:

    python3 -m simulator.attack --feeder ieee37
    python3 -m simulator.attack --feeder ieee123 --attack parameter_modification
    python3 -m simulator.attack --feeder ieee37 --seed 7 --modify-delta 0.50

Reads the real feeder model (``simulator.power.loader``) and its normal network
events CSV, runs every (or one) registered scenario through the
:class:`~simulator.attack.engine.AttackEngine` and prints the structured result
reports.  Exits non-zero if any scenario reports a FAILED result.

No files are written here -- persistence belongs to Task H.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from simulator.attack.base import AttackContext
from simulator.attack.engine import AttackEngine
from simulator.attack.scenarios import REGISTERED_SCENARIOS, scenario_id_for
from simulator.power.loader import FeederLoader

ROOT = Path(__file__).resolve().parents[2]


def _build_parser(attacks: List[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m simulator.attack",
        description="Run the registered MITRE-ICS attack scenarios against a feeder.",
    )
    parser.add_argument("--feeder", required=True, help="feeder id to attack")
    parser.add_argument("--attack", choices=attacks, default=None,
                        help="run a single attack (default: all registered)")
    parser.add_argument("--seed", type=int, default=42, help="selection seed")
    parser.add_argument("--timestamp", default="2010-07-01T00:00:00",
                        help="reference timestamp used by the scenarios")
    parser.add_argument("--results-dir", default=str(ROOT / "results"),
                        help="directory holding results/<feeder>/normal_*_events.csv")
    parser.add_argument("--modify-delta", type=float, default=0.35,
                        help="parameter-modification delta (new = baseline + delta)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(sorted(REGISTERED_SCENARIOS))
    args = parser.parse_args(argv)

    events_csv = (
        Path(args.results_dir)
        / args.feeder
        / f"normal_{args.feeder}_network_events.csv"
    )
    if not events_csv.exists():
        parser.error(
            f"no normal network events at {events_csv}; "
            "run the Phase E generator first (simulator/network/normal.py)"
        )

    model = FeederLoader().load(args.feeder)
    attack_names = [args.attack] if args.attack else sorted(REGISTERED_SCENARIOS)
    engine = AttackEngine(seed=args.seed)
    rc = 0
    for attack_name in attack_names:
        attack = REGISTERED_SCENARIOS[attack_name]
        context = AttackContext(
            scenario_id=scenario_id_for(attack.attack_id, args.feeder),
            feeder_id=args.feeder,
            model=model,
            event_path=events_csv,
            timestamp=args.timestamp,
            config={"modify_delta": args.modify_delta},
        )
        result = engine.run(attack, context)
        print("\n".join(result.report()))
        print("-" * 56)
        if result.result != "SUCCESS":
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())