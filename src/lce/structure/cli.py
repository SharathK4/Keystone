"""``lce structure`` - build, verify and print the value-of-speed artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def cmd_build(args: argparse.Namespace) -> int:
    from lce.benchmark.scenarios import ScenarioFamily
    from lce.structure.build import build_speed, save_speed

    families = tuple(ScenarioFamily(f) for f in args.families) if args.families else None
    payload = build_speed(snapshot_dir=Path(args.snapshot), seeds=tuple(args.seeds),
                          delays=tuple(args.delays), families=families)
    directory = save_speed(payload, Path(args.out))
    print(json.dumps({"artifact": str(directory), "n_scenarios": len(payload["scenarios"])}))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Load and hash-check; with ``--rebuild``, build again and compare hashes."""
    from lce.structure.store import SpeedStore, resolve_speed

    store = SpeedStore(resolve_speed(Path(args.out)))
    out: dict[str, Any] = {"artifact_id": store.artifact_id, "content_hash": store.content_hash,
                           "loads": True}
    if args.rebuild:
        from lce.structure.build import build_speed, content_hash

        p = store.provenance
        rebuilt = build_speed(snapshot_dir=Path(args.snapshot), seeds=tuple(p.seeds),
                              scale=p.scale, delays=tuple(p.delays_hours))
        out["rebuilt_hash"] = content_hash(rebuilt)
        out["identical"] = out["rebuilt_hash"] == store.content_hash
    print(json.dumps(out, indent=2))
    return 0 if out.get("identical", True) else 1


def cmd_show(args: argparse.Namespace) -> int:
    from lce.structure.store import SpeedStore, resolve_speed

    summary = SpeedStore(resolve_speed(Path(args.out))).summary()
    print(f"{summary.label} - {summary.n_with_plan} of {summary.n_scenarios} scenarios "
          f"have a plan; {summary.monotone_scenarios} fall monotonically")
    print("  delay   frozen cut %            replanned cut %         gain pp")
    for p in summary.points:
        f, r, g = p.frozen_cut_pct, p.replanned_cut_pct, p.replanning_gain_pp
        print(f"  {p.delay_hours:4g} h  {f.mean:6.2f} [{f.low:6.2f},{f.high:6.2f}]   "
              f"{r.mean:6.2f} [{r.low:6.2f},{r.high:6.2f}]   {g.mean:5.2f} [{g.low:5.2f},"
              f"{g.high:5.2f}]")
    return 0


def register(sub: Any) -> None:
    from lce.structure.build import DELAYS_HOURS, SEEDS
    from lce.structure.store import STRUCTURE_ROOT

    snapshot = str(Path("artifacts") / "snapshots" / "snap-a4c99d5a49a64ffd")
    st = sub.add_parser("structure", help="Structural results (value of speed)")
    ssub = st.add_subparsers(dest="structure_command", required=True)

    b = ssub.add_parser("build", help="Replay every scenario on the delay grid (offline)")
    b.add_argument("--snapshot", default=snapshot)
    b.add_argument("--out", default=str(STRUCTURE_ROOT))
    b.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS))
    b.add_argument("--delays", type=float, nargs="+", default=list(DELAYS_HOURS))
    b.add_argument("--families", nargs="+", default=None)
    b.set_defaults(func=cmd_build)

    v = ssub.add_parser("verify", help="Hash-check the artifact; --rebuild to reproduce it")
    v.add_argument("--snapshot", default=snapshot)
    v.add_argument("--out", default=str(STRUCTURE_ROOT))
    v.add_argument("--rebuild", action="store_true")
    v.set_defaults(func=cmd_verify)

    s = ssub.add_parser("show", help="Print the aggregate value of speed")
    s.add_argument("--out", default=str(STRUCTURE_ROOT))
    s.set_defaults(func=cmd_show)
