"""Runtime configuration, environment-driven."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("HDF_DATABASE_URL", "sqlite:///hdf.db")
    )
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
    exports_dir: str = field(default_factory=lambda: os.environ.get("HDF_EXPORTS_DIR", "exports"))


settings = Settings()
