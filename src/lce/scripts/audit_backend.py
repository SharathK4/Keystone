"""Backend freeze audit.

One pass over everything that has to be true before the backend is declared
frozen, each check independent and each failure specific. Exits non-zero if any
check fails, so it can gate a release.

    python src/lce/scripts/audit_backend.py

It never contacts Razorpay unless ``--live`` is passed, and it never prints a
credential: the secret checks assert *absence*, and report only field names and
counts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))


class Audit:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        (self.passed if ok else self.failed).append(
            name if ok else f"{name}: {detail}" if detail else name
        )
        return ok


# --------------------------------------------------------------------- secrets

#: Patterns that must never appear in a tracked file. The key-id pattern is the
#: documented Razorpay prefix; the generic ones catch a secret pasted into code.
SECRET_PATTERNS = (
    ("razorpay_key_id", re.compile(r"rzp_(live|test)_[A-Za-z0-9]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
)

#: Files allowed to mention a key *pattern* because they document or test it.
SECRET_ALLOWLIST = {
    "src/lce/scripts/audit_backend.py",
    "tests/test_razorpay.py",
    ".env.example",
    "docs/BACKEND_FREEZE_REPORT.md",
}


def audit_secrets(audit: Audit) -> None:
    """No credential may be committed, and .env must be ignored."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()

    offenders: list[str] = []
    for relative in tracked:
        if relative in SECRET_ALLOWLIST:
            continue
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{relative} ({label})")

    audit.check("secrets_not_committed", not offenders, "; ".join(offenders[:5]))
    audit.notes["files_scanned"] = len(tracked)

    ignored = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True
    )
    audit.check("env_is_gitignored", ignored.returncode == 0, "'.env' is not ignored")

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    audit.check(
        "no_env_file_staged",
        not any(line.strip().endswith(".env") for line in status.splitlines()),
        "a .env file appears in git status",
    )

    key_files = [p for p in (ROOT).glob("*.csv") if "key" in p.name.lower()]
    audit.check(
        "no_key_file_in_repo_root",
        not key_files,
        f"credential-looking files: {[p.name for p in key_files]}",
    )


# ----------------------------------------------------------------- serving


def audit_serving(audit: Audit) -> None:
    """The serving path must not import training or data-generation code."""
    code = (
        "import sys, json\n"
        "import lce.inference.service, lce.snapshot.store, lce.api.routers.analytics\n"
        "forbidden = ('lce.data.generator','lce.benchmark','lce.learning.dataset',"
        "'lce.learning.baselines','lce.learning.experiment','lce.intervention.experiment')\n"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if any(m == f or m.startswith(f + '.') for f in forbidden))))\n"
    )
    import os

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src", "LCE_LOG_LEVEL": "ERROR"},
    )
    if result.returncode != 0:
        audit.check("serving_imports_clean", False, result.stderr[-300:])
        return
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    audit.check("serving_imports_clean", leaked == [], f"leaked: {leaked}")


def audit_artifacts(audit: Audit, *, artifact_root: Path) -> None:
    """A model artifact and a snapshot must both load and verify."""
    from lce.errors import LCEError
    from lce.inference.artifact import load_artifact, resolve_artifact
    from lce.learning.features import FEATURE_SCHEMA_VERSION

    try:
        artifact = load_artifact(
            resolve_artifact(artifact_root), expected_schema=FEATURE_SCHEMA_VERSION
        )
        audit.check("model_artifact_loads", True)
        audit.check(
            "model_artifact_has_integrity_hash", bool(artifact.manifest.content_hash)
        )
        audit.check(
            "model_artifact_records_calibration",
            bool(artifact.manifest.calibrator.get("calibrator")),
        )
        audit.notes["model"] = artifact.summary()
    except LCEError as exc:
        audit.check("model_artifact_loads", False, exc.message)

    try:
        from lce.snapshot.store import SnapshotStore, resolve_snapshot

        store = SnapshotStore(resolve_snapshot(artifact_root / "snapshots"))
        audit.check("snapshot_loads", True)
        audit.check("snapshot_has_scenarios", store.manifest.n_scenarios > 0)
        audit.check(
            "snapshot_carries_no_payment_history",
            store.graph.stats().n_payment_events == 0,
            "the serving graph should hold no event stream",
        )
        audit.check(
            "snapshot_overlay_is_estimated",
            all(not e.is_ground_truth for e in store.graph.dependency_edges),
            "the snapshot must not ship generator ground truth",
        )
        audit.check(
            "every_scenario_has_provenance",
            all(
                s.provenance.dataset_version
                and s.provenance.config_hash
                and s.provenance.simulator_config_hash
                for s in store.scenarios()
            ),
        )
        audit.check(
            "on_demand_analysis_is_bounded",
            store.analysis_limits()["max_candidates"] <= 24
            and store.analysis_limits()["max_actions"] <= 3,
        )
        audit.notes["snapshot"] = store.health()
    except LCEError as exc:
        audit.check("snapshot_loads", False, exc.message)


def audit_determinism(audit: Audit, *, artifact_root: Path) -> None:
    """The same request must produce the same answer, twice."""
    from lce.snapshot.store import SnapshotStore, resolve_snapshot

    try:
        store = SnapshotStore(resolve_snapshot(artifact_root / "snapshots"))
    except Exception as exc:  # already reported by audit_artifacts
        audit.check("determinism", False, str(exc)[:200])
        return

    target = store.merchants()[0].merchant_id
    first = store.analyze(merchant_ids=[target], magnitude_multiple=2.0)
    second = store.analyze(merchant_ids=[target], magnitude_multiple=2.0)
    audit.check(
        "on_demand_analysis_is_deterministic",
        first.scenario_id == second.scenario_id
        and first.counterfactual.model_dump() == second.counterfactual.model_dump(),
    )


def audit_api(audit: Audit) -> None:
    """Every documented route must exist and the schema must publish."""
    from fastapi.testclient import TestClient

    from lce.api.app import create_app

    required = {
        "/api/v1/network",
        "/api/v1/network/merchants",
        "/api/v1/network/merchants/{merchant_id}",
        "/api/v1/network/dependencies",
        "/api/v1/network/systemic-importance",
        "/api/v1/scenarios",
        "/api/v1/scenarios/{scenario_id}",
        "/api/v1/scenarios/{scenario_id}/impact",
        "/api/v1/scenarios/{scenario_id}/interventions",
        "/api/v1/scenarios/{scenario_id}/counterfactual",
        "/api/v1/scenarios/analyze",
        "/api/v1/scenarios/replay",
        "/api/v1/offers",
        "/api/v1/offers/{merchant_id}",
        "/api/v1/dashboard",
        "/api/v1/execution/status",
        "/api/v1/snapshot",
        "/api/v1/model",
        "/api/v1/predict/contagion",
        "/api/v1/interventions/recommend",
        "/api/v1/health",
    }
    app = create_app()
    published = {getattr(r, "path", "") for r in app.routes}
    missing = sorted(required - published)
    audit.check("all_frontend_routes_present", not missing, f"missing: {missing}")

    with TestClient(app) as client:
        schema = client.get("/openapi.json")
        audit.check("openapi_schema_publishes", schema.status_code == 200)
        audit.notes["n_routes"] = len(
            [p for p in published if isinstance(p, str) and p.startswith("/api/v1")]
        )


def audit_migrations(audit: Audit) -> None:
    """Alembic revisions must form a single unbroken chain."""
    versions = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    revisions: dict[str, str | None] = {}
    for path in versions:
        text = path.read_text(encoding="utf-8")
        revision = re.search(r"^revision(?::\s*str)?\s*=\s*['\"]([^'\"]+)", text, re.M)
        down = re.search(
            r"^down_revision(?::[^=]*)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", text, re.M
        )
        if revision:
            revisions[revision.group(1)] = down.group(1) if down and down.group(1) else None

    audit.check("migrations_present", bool(revisions), "no alembic revisions found")
    roots = [r for r, down in revisions.items() if down is None]
    audit.check("migrations_have_one_root", len(roots) == 1, f"roots: {roots}")
    dangling = [
        down for down in revisions.values() if down is not None and down not in revisions
    ]
    audit.check("migrations_chain_unbroken", not dangling, f"missing parents: {dangling}")
    heads = set(revisions) - {d for d in revisions.values() if d}
    audit.check("migrations_have_one_head", len(heads) <= 1, f"heads: {sorted(heads)}")
    audit.notes["migrations"] = {"n_revisions": len(revisions), "heads": sorted(heads)}


def audit_razorpay(audit: Audit, *, live: bool) -> None:
    """Provider assumptions must be probed, not inferred; live mode refused."""
    from lce.config import RazorpayMode, RazorpaySettings
    from lce.errors import ConfigError
    from lce.execution.providers import RazorpayTestProvider

    refused = False
    try:
        RazorpayTestProvider(
            settings=RazorpaySettings(
                RAZORPAY_KEY_ID="rzp_live_x", RAZORPAY_KEY_SECRET="x",
                RAZORPAY_MODE=RazorpayMode.LIVE,
            )
        )
    except ConfigError:
        refused = True
    audit.check("live_mode_refused", refused, "live mode was accepted")

    if not live:
        audit.notes["razorpay"] = {"probed": False, "note": "pass --live to probe"}
        return

    provider = RazorpayTestProvider()
    capabilities = provider.capabilities(refresh=True)
    audit.check(
        "razorpay_test_mode_reachable",
        capabilities.get("api_reachable", False),
        "Test Mode API not reachable with the configured credentials",
    )
    audit.check(
        "route_not_assumed",
        capabilities.get("transfers") is not None,
        "transfers capability was not probed",
    )
    audit.notes["razorpay"] = {"probed": True, "capabilities": capabilities}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backend freeze audit")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument(
        "--live", action="store_true", help="Probe the real Razorpay Test Mode API."
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from lce.logging import configure_logging

    configure_logging(level="ERROR", force=True)

    audit = Audit()
    artifact_root = (ROOT / args.artifacts).resolve()

    audit_secrets(audit)
    audit_serving(audit)
    audit_artifacts(audit, artifact_root=artifact_root)
    audit_determinism(audit, artifact_root=artifact_root)
    audit_api(audit)
    audit_migrations(audit)
    audit_razorpay(audit, live=args.live)

    payload = {
        "passed": audit.passed,
        "failed": audit.failed,
        "notes": audit.notes,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for name in audit.passed:
            print(f"  PASS  {name}")
        for name in audit.failed:
            print(f"  FAIL  {name}")
        print()
        for key, value in audit.notes.items():
            print(f"  {key}: {json.dumps(value, default=str)[:300]}")

    print(f"\n{'=' * 72}")
    if audit.failed:
        print(f"AUDIT FAILED - {len(audit.failed)} check(s)")
        return 1
    print(f"BACKEND AUDIT CLEAN ({len(audit.passed)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
