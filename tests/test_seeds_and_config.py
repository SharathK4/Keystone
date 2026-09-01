"""Determinism guarantees and configuration handling."""

from __future__ import annotations

import os

import numpy as np
import pytest

from lce.config import RazorpayMode, RazorpaySettings, Settings, redact_dsn
from lce.errors import ConfigError
from lce.seeds import (
    build_seed_bundle,
    canonical_json,
    config_hash,
    derive_seed,
    rng,
    seed_everything,
)


class TestSeeds:
    def test_derive_seed_is_stable_across_processes(self):
        # SHA-256 rather than hash(): PYTHONHASHSEED is randomised per process,
        # so hash() would silently break reproducibility between runs.
        assert derive_seed(42, "topology") == derive_seed(42, "topology")
        assert derive_seed(42, "topology") != derive_seed(42, "behaviour")
        assert derive_seed(42, "topology") != derive_seed(43, "topology")

    def test_derive_seed_is_in_uint32_range(self):
        for base in (0, 1, 2**31, 2**32 - 1):
            value = derive_seed(base, "x", 1, [2, 3])
            assert 0 <= value <= 2**32 - 1

    def test_config_hash_ignores_dict_ordering(self):
        assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
        assert config_hash({"a": 1}) != config_hash({"a": 2})

    def test_canonical_json_is_sorted_and_compact(self):
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_seed_bundle_streams_are_distinct_and_reproducible(self):
        bundle = build_seed_bundle(1234, "abc")
        again = build_seed_bundle(1234, "abc")
        assert bundle == again

        values = set(bundle.to_dict().values()) - {bundle.base_seed}
        assert len(values) >= 5  # streams must not collide

        first = bundle.generator("events").random(5)
        second = again.generator("events").random(5)
        assert np.allclose(first, second)
        other = bundle.generator("shocks").random(5)
        assert not np.allclose(first, other)

    def test_generators_from_the_same_seed_agree(self):
        assert np.allclose(rng(7).normal(size=4), rng(7).normal(size=4))

    def test_seed_everything_makes_globals_deterministic(self):
        seed_everything(11)
        a = np.random.rand(3)
        seed_everything(11)
        assert np.allclose(a, np.random.rand(3))


class TestSettings:
    def test_secrets_are_not_exposed_by_safe_dump(self):
        settings = Settings(
            RAZORPAY_KEY_ID="rzp_test_abc",
            _env_file=None,
        )
        dumped = settings.safe_dump()
        assert "key_secret" not in dumped
        assert "***" in dumped["database_url"] or "@" not in dumped["database_url"]

    def test_secret_str_is_redacted_in_repr(self):
        settings = RazorpaySettings(
            RAZORPAY_KEY_ID="k", RAZORPAY_KEY_SECRET="super-secret", _env_file=None
        )
        assert "super-secret" not in repr(settings)
        assert settings.key_secret.get_secret_value() == "super-secret"

    def test_unconfigured_credentials_raise_only_when_required(self):
        settings = RazorpaySettings(_env_file=None)
        assert not settings.configured
        with pytest.raises(ConfigError):
            settings.require_credentials()

    def test_missing_webhook_secret_raises(self):
        settings = RazorpaySettings(_env_file=None)
        assert not settings.webhook_configured
        with pytest.raises(ConfigError):
            settings.require_webhook_secret()

    def test_live_mode_requires_explicit_opt_in(self, monkeypatch):
        # A misconfigured deployment must not silently point at live money.
        # Nested settings read the environment, so the guard is exercised there.
        monkeypatch.setenv("RAZORPAY_MODE", "live")
        monkeypatch.delenv("LCE_ALLOW_LIVE", raising=False)
        with pytest.raises(ValueError, match="LCE_ALLOW_LIVE"):
            Settings(_env_file=None)

        monkeypatch.setenv("LCE_ALLOW_LIVE", "true")
        allowed = Settings(_env_file=None)
        assert allowed.razorpay.mode is RazorpayMode.LIVE
        assert allowed.allow_live

    def test_invalid_log_level_is_rejected(self):
        with pytest.raises(ValueError):
            Settings(LCE_LOG_LEVEL="chatty", _env_file=None)

    def test_cors_origins_accept_a_comma_separated_string(self):
        settings = Settings(LCE_CORS_ORIGINS="http://a.test, http://b.test", _env_file=None)
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    @pytest.mark.parametrize(
        ("dsn", "expected"),
        [
            ("postgresql+psycopg://user:pw@host:5432/db", "postgresql+psycopg://user:***@host:5432/db"),
            ("postgresql://user@host/db", "postgresql://user@host/db"),
            ("sqlite:///./local.db", "sqlite:///./local.db"),
        ],
    )
    def test_dsn_password_is_redacted(self, dsn, expected):
        assert redact_dsn(dsn) == expected


class TestEnvironmentWiring:
    def test_test_suite_runs_against_a_throwaway_database(self):
        assert os.environ["DATABASE_URL"].startswith("sqlite")

    def test_env_var_overrides_reach_nested_settings(self):
        settings = Settings(_env_file=None)
        assert settings.simulation.horizon_hours > 0
        assert settings.objective.gamma_default >= 0
