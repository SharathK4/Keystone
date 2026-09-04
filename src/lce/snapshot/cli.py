"""Snapshot CLI: build the analytical artifact the API serves.

    lce snapshot build --seed 2025 --out artifacts/snapshots
    lce snapshot list  --root artifacts/snapshots
    lce snapshot info  --root artifacts/snapshots

``build`` is the only expensive command and the only one the API never calls.
Everything the frontend reads is produced here, once, offline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_build(args: argparse.Namespace) -> int:
    """Run the analysis once and write a servable snapshot."""
    from lce.benchmark.scenarios import ScenarioFamily
    from lce.intervention.profiles import ResourceProfile
    from lce.snapshot.build import build_snapshot
    from lce.snapshot.store import save_snapshot

    overrides = json.loads(args.generator_overrides) if args.generator_overrides else None
    payload, manifest = build_snapshot(
        seed=args.seed,
        scale=args.scale,
        profile=ResourceProfile(args.profile),
        families=tuple(ScenarioFamily(f) for f in args.families) if args.families else None,
        magnitude=args.magnitude,
        systemic_sample=args.systemic_sample,
        generator_overrides=overrides,
    )
    directory = save_snapshot(payload, manifest, Path(args.out) / manifest.snapshot_id)
    _print(
        {
            "snapshot_id": manifest.snapshot_id,
            "directory": str(directory),
            "dataset_version": manifest.dataset_version,
            "n_scenarios": manifest.n_scenarios,
            "content_hash": manifest.content_hash[:16],
            "build": manifest.build,
        }
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from lce.snapshot.store import list_snapshots

    _print({"root": args.root, "snapshots": list_snapshots(Path(args.root))})
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Load a snapshot the way the API does and report what came back."""
    from lce.snapshot.store import SnapshotStore, resolve_snapshot

    store = SnapshotStore(resolve_snapshot(Path(args.root), args.snapshot_id))
    _print(
        {
            **store.health(),
            "scenarios": [s.model_dump() for s in store.scenario_summaries()],
        }
    )
    return 0


def register(subparsers: Any) -> None:
    from lce.benchmark.scenarios import ScenarioFamily
    from lce.intervention.profiles import ResourceProfile

    group = subparsers.add_parser("snapshot", help="Analytical snapshots for the API")
    sub = group.add_subparsers(dest="snapshot_command", required=True)

    build = sub.add_parser("build", help="Build a servable analytical snapshot")
    build.add_argument("--seed", type=int, default=2025)
    build.add_argument("--scale", default="small", choices=["small", "medium", "large"])
    build.add_argument(
        "--profile",
        default=str(ResourceProfile.SMALL_FAST),
        choices=[p.value for p in ResourceProfile],
    )
    build.add_argument(
        "--families", nargs="*", default=None, choices=[f.value for f in ScenarioFamily]
    )
    build.add_argument("--magnitude", type=float, default=2.0)
    build.add_argument(
        "--systemic-sample", type=int, default=None, dest="systemic_sample",
        help="Merchants covered by the systemic sweep; defaults to the profile's cap.",
    )
    build.add_argument("--out", default="artifacts/snapshots")
    build.add_argument(
        "--generator-overrides", default=None, dest="generator_overrides",
        help='JSON generator overrides, e.g. \'{"n_merchants": 60}\'.',
    )
    build.set_defaults(func=cmd_build)

    listing = sub.add_parser("list", help="List built snapshots")
    listing.add_argument("--root", default="artifacts/snapshots")
    listing.set_defaults(func=cmd_list)

    info = sub.add_parser("info", help="Load a snapshot and report it")
    info.add_argument("--root", default="artifacts/snapshots")
    info.add_argument("--snapshot-id", default=None, dest="snapshot_id")
    info.set_defaults(func=cmd_info)
