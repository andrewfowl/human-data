"""Runtime configuration, environment-driven."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_ON_VERCEL = bool(os.environ.get("VERCEL"))


def _default_database_url() -> str:
    # Precedence: explicit HDF_DATABASE_URL → platform-provided Postgres URL
    # (Vercel/Neon/Supabase conventions) → local sqlite (or /tmp on Vercel,
    # where only /tmp is writable — ephemeral demo mode).
    url = (
        os.environ.get("HDF_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if url:
        # SQLAlchemy needs the postgresql:// scheme (Heroku/Vercel emit postgres://).
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    return "sqlite:////tmp/hdf.db" if _ON_VERCEL else "sqlite:///hdf.db"


@dataclass
class Settings:
    database_url: str = field(default_factory=_default_database_url)
    # Autonomous QC reviewer model. Opus-tier by default; override with HDF_QC_MODEL.
    qc_model: str = field(default_factory=lambda: os.environ.get("HDF_QC_MODEL", "claude-opus-4-8"))
    # When no Anthropic credential is available the QC engine falls back to the
    # deterministic heuristic reviewer (flagged OFFLINE_FALLBACK in the review record).
    qc_offline: bool = field(
        default_factory=lambda: os.environ.get("HDF_QC_OFFLINE", "").lower() in ("1", "true", "yes")
    )
    # Auto-QC score thresholds (0-5 scale).
    qc_pass_threshold: float = 4.0   # >= : eligible for auto-approval (subject to sampling)
    qc_gray_threshold: float = 3.0   # >= and < pass : mandatory human review
    # Human-review sampling rates by contributor trust tier.
    sampling_new: float = 1.00       # < 5 approved submissions: review everything
    sampling_standard: float = 0.35  # quality score < 4.2
    sampling_trusted: float = 0.10   # floor — never sample below this
    trusted_score: float = 4.2
    trusted_min_approved: int = 20
    # Contributor scoring
    score_ewma_alpha: float = 0.3
    gold_fail_penalty: float = 0.75  # multiplier applied to quality score on a missed gold task
    exports_dir: str = field(
        default_factory=lambda: os.environ.get(
            "HDF_EXPORTS_DIR", "/tmp/hdf-exports" if _ON_VERCEL else "exports"
        )
    )
    # --- auth ------------------------------------------------------------
    # Bearer API-key auth is ON by default. HDF_AUTH_DISABLED=1 switches to
    # dev mode (X-Actor-Id header, admin role) for local work and tests.
    auth_disabled: bool = field(
        default_factory=lambda: os.environ.get("HDF_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    )
    # --- billing ----------------------------------------------------------
    currency: str = field(default_factory=lambda: os.environ.get("HDF_CURRENCY", "usd"))
    # Default per-approved-record rates in cents, used when a firm has no rate card.
    default_rates_cents: dict = field(default_factory=lambda: {
        "sft": int(os.environ.get("HDF_RATE_SFT_CENTS", "12000")),
        "preference": int(os.environ.get("HDF_RATE_PREFERENCE_CENTS", "15000")),
        "eval": int(os.environ.get("HDF_RATE_EVAL_CENTS", "18000")),
    })
    stripe_secret_key: str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", ""))
    stripe_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    )


settings = Settings()
