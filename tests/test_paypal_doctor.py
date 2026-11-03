"""``lce paypal doctor``: what is missing, never a secret, PayPal only on request."""

from __future__ import annotations

import pytest

from lce.config import reset_paypal_settings_cache, reset_settings_cache
from lce.paypal import doctor
from lce.walk.thread import reset_walk_settings_cache


@pytest.fixture(autouse=True)
def _fresh():
    reset_paypal_settings_cache()
    reset_settings_cache()
    reset_walk_settings_cache()
    yield
    reset_paypal_settings_cache()
    reset_settings_cache()
    reset_walk_settings_cache()


def _by_name(checks):
    return {c.name: c for c in checks}


def test_nothing_configured_says_what_to_do(monkeypatch):
    monkeypatch.setattr(doctor, "_walk_plan_merchants", lambda: ("m0038", ["m0005", "m0009"]))
    checks = _by_name(doctor.diagnose())
    assert checks["credentials"].status == "missing" and checks["credentials"].fix
    assert checks["webhook_id"].status == "missing"
    assert checks["token"].status == "unchecked"
    assert checks["public_url"].status == "unchecked"
    for merchant in ("m0038", "m0005", "m0009"):
        assert checks[f"identity:{merchant}"].status == "missing"
        assert f"paypal map {merchant}" in (checks[f"identity:{merchant}"].fix or "")
    assert "unclaimed" in checks["identity:m0038"].detail


def test_credentials_are_never_printed(monkeypatch, capsys):
    from lce.paypal.cli import cmd_doctor

    monkeypatch.setenv("PAYPAL_CLIENT_ID", "client-id-must-not-appear")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret-must-not-appear")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "webhook-id-must-not-appear")
    monkeypatch.setattr(doctor, "_walk_plan_merchants", lambda: ("m0038", ["m0005"]))

    class Args:
        probe = False
        public_url = None

    cmd_doctor(Args())  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "must-not-appear" not in out
    assert "ok        credentials" in out


def test_no_call_to_paypal_without_probe(monkeypatch):
    from lce.paypal import client as client_module

    def boom(*a, **k):
        raise AssertionError("PayPal was called")

    monkeypatch.setattr(client_module.PayPalClient, "probe_token", boom)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    monkeypatch.setattr(doctor, "_walk_plan_merchants", lambda: ("m0038", []))
    assert _by_name(doctor.diagnose())["token"].status == "unchecked"


def test_the_walk_plan_payees_come_from_the_replay(monkeypatch):
    from pathlib import Path

    from lce.snapshot import store as store_module

    canonical = Path(__file__).resolve().parents[1] / "artifacts" / "snapshots" / \
        "snap-a4c99d5a49a64ffd"
    snapshot = store_module.SnapshotStore(canonical)
    monkeypatch.setattr(store_module, "require_store", lambda: snapshot)
    merchant, payees = doctor._walk_plan_merchants()
    assert merchant == "m0038"
    assert payees == ["m0005", "m0009"]
