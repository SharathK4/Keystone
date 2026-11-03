"""Environment-driven application configuration.

All configuration is read from the process environment (optionally seeded from a
``.env`` file for local development). Secrets are held as ``SecretStr`` so they
are redacted from reprs, logs and tracebacks. Nothing is hard-coded.
"""

from __future__ import annotations

import functools
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lce.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class RazorpayMode(StrEnum):
    TEST = "test"
    LIVE = "live"


class RazorpaySettings(BaseSettings):
    """Razorpay integration credentials and mode.

    Credentials are optional so the research/simulation half of the system runs
    with no Razorpay account at all; :meth:`require_credentials` is called only
    by code paths that actually hit the API.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    key_id: str = Field(default="", alias="RAZORPAY_KEY_ID")
    key_secret: SecretStr = Field(default=SecretStr(""), alias="RAZORPAY_KEY_SECRET")
    webhook_secret: SecretStr = Field(default=SecretStr(""), alias="RAZORPAY_WEBHOOK_SECRET")
    account_id: str = Field(default="", alias="RAZORPAY_ACCOUNT_ID")
    mode: RazorpayMode = Field(default=RazorpayMode.TEST, alias="RAZORPAY_MODE")
    api_base_url: str = Field(default="https://api.razorpay.com/v1", alias="RAZORPAY_API_BASE_URL")
    timeout_seconds: float = Field(default=10.0, alias="RAZORPAY_TIMEOUT_SECONDS", gt=0)

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret.get_secret_value())

    @property
    def webhook_configured(self) -> bool:
        return bool(self.webhook_secret.get_secret_value())

    def require_credentials(self) -> tuple[str, str]:
        if not self.configured:
            raise ConfigError(
                "Razorpay API credentials are not configured; "
                "set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
                mode=str(self.mode),
            )
        return self.key_id, self.key_secret.get_secret_value()

    def require_webhook_secret(self) -> str:
        if not self.webhook_configured:
            raise ConfigError("RAZORPAY_WEBHOOK_SECRET is not configured.")
        return self.webhook_secret.get_secret_value()


class PayPalMode(StrEnum):
    SANDBOX = "sandbox"
    LIVE = "live"


#: Hosts a PayPal API base may point at. Anything else - including PayPal's own
#: production hosts - is refused, so a wrong URL cannot turn a sandbox build into
#: one that moves real money.
PAYPAL_SANDBOX_HOSTS = frozenset({"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"})


class PayPalSettings(BaseSettings):
    """PayPal sandbox credentials, demo scale and guardrails.

    Deliberately **not** nested inside :class:`Settings`. The analysis must serve
    with no PayPal account at all, and a misconfigured PayPal section must never
    stop it from starting; only code paths that talk to PayPal load this.

    Every credential is a :class:`SecretStr`, including the client id and the
    webhook id, so none of them can reach a repr, a log line or a traceback.

    Sandbox only. ``PAYPAL_MODE=live`` parses (so status can say *why* it is
    refused) but :meth:`require_sandbox` raises on it, and there is no override:
    unlike Razorpay, no ``LCE_ALLOW_LIVE`` switch enables live PayPal.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    client_id: SecretStr = Field(default=SecretStr(""), alias="PAYPAL_CLIENT_ID")
    client_secret: SecretStr = Field(default=SecretStr(""), alias="PAYPAL_CLIENT_SECRET")
    webhook_id: SecretStr = Field(default=SecretStr(""), alias="PAYPAL_WEBHOOK_ID")
    mode: PayPalMode = Field(default=PayPalMode.SANDBOX, alias="PAYPAL_MODE")
    # Also accept events from PayPal's webhook simulator, which signs with the
    # literal id "WEBHOOK_ID". Off by default; turn on only while testing.
    accept_simulator_events: bool = Field(default=False, alias="PAYPAL_ACCEPT_SIMULATOR_EVENTS")
    api_base: str = Field(default="https://api-m.sandbox.paypal.com", alias="PAYPAL_API_BASE")
    timeout_seconds: float = Field(default=15.0, alias="PAYPAL_TIMEOUT_SECONDS", gt=0)

    # --- demo scale -----------------------------------------------------------
    # The analysis is in rupees and PayPal cannot pay out INR, so execution runs
    # in a sandbox currency at one declared scale: units of `demo_currency` per
    # analytic rupee. The default, 0.00001, is one US dollar per rupee lakh.
    demo_currency: str = Field(default="USD", alias="PAYPAL_DEMO_CURRENCY")
    demo_scale: Decimal = Field(default=Decimal("0.00001"), alias="PAYPAL_DEMO_SCALE", gt=0)

    @field_validator("demo_currency")
    @classmethod
    def _iso_currency(cls, v: str) -> str:
        code = v.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(f"PAYPAL_DEMO_CURRENCY must be an ISO-4217 code, got {v!r}")
        if code == "INR":
            raise ValueError("PayPal cannot pay out INR; choose a supported sandbox currency")
        return code

    @property
    def configured(self) -> bool:
        return bool(self.client_id.get_secret_value() and self.client_secret.get_secret_value())

    @property
    def webhook_configured(self) -> bool:
        return bool(self.webhook_id.get_secret_value())

    @property
    def api_host(self) -> str:
        return urlparse(self.api_base).hostname or ""

    def refusal(self) -> str | None:
        """Why this configuration must not be used, or ``None`` if it may."""
        if self.mode is PayPalMode.LIVE:
            return "PAYPAL_MODE=live is refused: this build is sandbox-only"
        parsed = urlparse(self.api_base)
        if parsed.scheme != "https" or (parsed.hostname or "") not in PAYPAL_SANDBOX_HOSTS:
            return f"PAYPAL_API_BASE host {parsed.hostname!r} is not a PayPal sandbox host"
        return None

    def require_sandbox(self) -> None:
        reason = self.refusal()
        if reason is not None:
            raise ConfigError(reason, mode=str(self.mode))

    def require_credentials(self) -> tuple[str, str]:
        self.require_sandbox()
        if not self.configured:
            raise ConfigError(
                "PayPal sandbox credentials are not configured; "
                "set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET."
            )
        return self.client_id.get_secret_value(), self.client_secret.get_secret_value()

    def require_webhook_id(self) -> str:
        if not self.webhook_configured:
            raise ConfigError("PAYPAL_WEBHOOK_ID is not configured.")
        return self.webhook_id.get_secret_value()

    def safe_dump(self) -> dict[str, object]:
        """Everything except credentials - safe to log or return."""
        return {
            "mode": str(self.mode),
            "api_host": self.api_host,
            "configured": self.configured,
            "webhook_configured": self.webhook_configured,
            "demo_currency": self.demo_currency,
            "demo_scale": str(self.demo_scale),
            "refused": self.refusal(),
        }


class SimulationSettings(BaseSettings):
    """Default economic constants for the liquidity simulator.

    These are *defaults*; every experiment config may override them, and the
    override is recorded in the run manifest.
    """

    model_config = SettingsConfigDict(env_prefix="LCE_SIM_", extra="ignore")

    horizon_hours: float = Field(default=168.0, gt=0, description="Default simulation horizon T.")
    tick_hours: float = Field(default=1.0, gt=0, description="Discretisation step for integrals.")
    grace_period_hours: float = Field(
        default=48.0, ge=0, description="Hours past deadline before a miss becomes a default."
    )
    default_credit_line_ratio: float = Field(
        default=0.15, ge=0, description="Credit line as a fraction of opening balance."
    )
    partial_payment_enabled: bool = Field(
        default=True, description="Allow settling an obligation partially when cash is short."
    )
    min_partial_fraction: float = Field(
        default=0.10, ge=0, le=1, description="Below this fraction of the amount, do not part-pay."
    )


class ObjectiveSettings(BaseSettings):
    """Weights of the network disruption objective D(G, S)."""

    model_config = SettingsConfigDict(env_prefix="LCE_OBJ_", extra="ignore")

    gamma_delay: float = Field(default=1.0, ge=0, description="Weight on value-weighted delay.")
    gamma_default: float = Field(default=500000.0, ge=0, description="Weight per default event.")
    gamma_deficit: float = Field(
        default=0.02, ge=0, description="Weight on liquidity deficit-time."
    )
    delay_unit_hours: float = Field(default=24.0, gt=0, description="phi() delay normaliser.")
    discount_rate_per_hour: float = Field(
        default=0.0, ge=0, description="Exponential discount applied to disruption over time."
    )


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- core ---------------------------------------------------------------
    env: Environment = Field(default=Environment.LOCAL, alias="LCE_ENV")
    app_name: str = Field(default="liquidity-contagion-engine", alias="LCE_APP_NAME")

    database_url: str = Field(
        default="postgresql+psycopg://lce:lce@localhost:5432/lce", alias="DATABASE_URL"
    )
    db_echo: bool = Field(default=False, alias="LCE_DB_ECHO")
    db_pool_size: int = Field(default=5, ge=1, alias="LCE_DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, ge=0, alias="LCE_DB_MAX_OVERFLOW")

    model_artifact_dir: Path = Field(default=REPO_ROOT / "artifacts", alias="MODEL_ARTIFACT_DIR")
    random_seed: int = Field(default=20250101, ge=0, alias="RANDOM_SEED")

    # --- api ----------------------------------------------------------------
    api_host: str = Field(default="0.0.0.0", alias="LCE_API_HOST")
    api_port: int = Field(default=8000, ge=1, le=65535, alias="LCE_API_PORT")
    api_root_path: str = Field(default="", alias="LCE_API_ROOT_PATH")
    cors_origins: list[str] = Field(default_factory=list, alias="LCE_CORS_ORIGINS")

    # --- logging ------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LCE_LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="json", alias="LCE_LOG_FORMAT")

    # --- guardrails ---------------------------------------------------------
    allow_live: bool = Field(default=False, alias="LCE_ALLOW_LIVE")
    # Which provider /execution/status describes. The default keeps the
    # behaviour this API shipped with; "paypal_sandbox" switches it to PayPal.
    execution_provider: Literal["razorpay_test", "paypal_sandbox"] = Field(
        default="razorpay_test", alias="LCE_EXECUTION_PROVIDER"
    )

    # --- nested -------------------------------------------------------------
    razorpay: RazorpaySettings = Field(default_factory=RazorpaySettings)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    objective: ObjectiveSettings = Field(default_factory=ObjectiveSettings)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return level

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("model_artifact_dir", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _guard_live_mode(self) -> Settings:
        if self.razorpay.mode is RazorpayMode.LIVE and not self.allow_live:
            raise ValueError(
                "RAZORPAY_MODE=live requires LCE_ALLOW_LIVE=true. "
                "Refusing to start against live Razorpay credentials by accident."
            )
        return self

    @property
    def is_test(self) -> bool:
        return self.env is Environment.TEST

    def ensure_artifact_dir(self) -> Path:
        self.model_artifact_dir.mkdir(parents=True, exist_ok=True)
        return self.model_artifact_dir

    def safe_dump(self) -> dict[str, object]:
        """Config snapshot with secrets redacted - safe to log or persist."""
        return {
            "env": str(self.env),
            "app_name": self.app_name,
            "database_url": redact_dsn(self.database_url),
            "model_artifact_dir": str(self.model_artifact_dir),
            "random_seed": self.random_seed,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "razorpay_mode": str(self.razorpay.mode),
            "razorpay_configured": self.razorpay.configured,
            "razorpay_webhook_configured": self.razorpay.webhook_configured,
            "simulation": self.simulation.model_dump(),
            "objective": self.objective.model_dump(),
        }


def redact_dsn(dsn: str) -> str:
    """Strip the password out of a SQLAlchemy/libpq URL before logging it."""
    if "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    if "@" not in rest:
        return dsn
    creds, _, host = rest.rpartition("@")
    user, sep, _pw = creds.partition(":")
    suffix = ":***" if sep else ""
    return f"{scheme}://{user}{suffix}@{host}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (tests only)."""
    get_settings.cache_clear()


@functools.lru_cache(maxsize=1)
def get_paypal_settings() -> PayPalSettings:
    """PayPal settings, loaded only by code that talks to PayPal."""
    return PayPalSettings()


def reset_paypal_settings_cache() -> None:
    get_paypal_settings.cache_clear()


class DemoSettings(BaseSettings):
    """Guardrails for a publicly hosted demo.

    ``LCE_DEMO_MODE=recorded`` (what ``render.yaml`` sets) shows the ledger of a
    sandbox run recorded earlier and refuses to start a new one unless the
    request carries the demo token. A token holder is rate-limited per address
    and can execute only the canonical plan. ``off`` (the default) changes
    nothing, so local development and the tests behave as before.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    mode: Literal["off", "recorded"] = Field(default="off", alias="LCE_DEMO_MODE")
    token: SecretStr = Field(default=SecretStr(""), alias="LCE_DEMO_TOKEN")
    scenario_id: str = Field(default="scn-07c647ec1459", alias="LCE_DEMO_SCENARIO")
    rate_limit: int = Field(default=6, ge=1, alias="LCE_DEMO_RATE_LIMIT")
    rate_window_s: int = Field(default=600, ge=1, alias="LCE_DEMO_RATE_WINDOW_S")


@functools.lru_cache(maxsize=1)
def get_demo_settings() -> DemoSettings:
    return DemoSettings()


def reset_demo_settings_cache() -> None:
    get_demo_settings.cache_clear()


class DisplaySettings(BaseSettings):
    """How the interface prints money. Presentation only; nothing is computed in it.

    The synthetic network is generated in rupees. A reader at a US payment
    platform should not have to convert crore in their head, so amounts are shown
    in US dollars by default at one declared rate (``LCE_DISPLAY_USD_PER_INR``).
    It is a round display rate, not a market quote, and the interface says so.
    ``LCE_DISPLAY_CURRENCY=INR`` restores rupees.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    currency: Literal["USD", "INR"] = Field(default="USD", alias="LCE_DISPLAY_CURRENCY")
    usd_per_inr: Decimal = Field(default=Decimal("0.01"), gt=0, alias="LCE_DISPLAY_USD_PER_INR")


@functools.lru_cache(maxsize=1)
def get_display_settings() -> DisplaySettings:
    return DisplaySettings()


def reset_display_settings_cache() -> None:
    get_display_settings.cache_clear()
