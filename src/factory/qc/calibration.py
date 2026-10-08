"""Calibration of the autonomous QC reviewer against human judgment.

Every submission that reaches human review carries both an auto-LLM verdict
and a human verdict — that overlap is a continuously-growing calibration set.
This module measures how well the autonomous reviewer tracks human judgment:

  verdict agreement   — did auto pass/hold match the human pass/not-pass?
  false pass          — auto passed, human said revise/fail (the dangerous
                        direction: without sampling this record would ship)
  false hold          — auto held, human passed (costs review time, not quality)
  score agreement     — exact and adjacent (±1) level match where both sides
                        produced an overall 0–5 score

Results are sliced by domain track and grader version, and translated into a
sampling recommendation that feeds control C3:

  raise_sampling   false-pass rate above tolerance → widen human review
  hold             not enough evidence either way, or mixed signals
  consider_lower   strong agreement on a sufficient sample → the trusted-tier
                   floor can be defended at its current level or reviewed
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Project, Review, ReviewKind, Submission, Task, Verdict

FALSE_PASS_TOLERANCE = 0.05
MIN_PAIRS_FOR_CONFIDENCE = 50
STRONG_AGREEMENT = 0.90


def _pairs(db: Session, track: str | None = None):
    """Yield (submission, auto_review, human_review, track) calibration pairs."""
    rows = db.execute(
        select(Submission, Task, Project)
        .join(Task, Submission.task_id == Task.id)
        .join(Project, Task.project_id == Project.id)
        .where(Project.track == track if track else True)
    ).all()
    for sub, task, project in rows:
        reviews = db.execute(
            select(Review).where(Review.submission_id == sub.id)
            .order_by(Review.created_at)
        ).scalars().all()
        auto = next((r for r in reversed(reviews)
                     if r.kind == ReviewKind.AUTO_LLM.value), None)
        human = next((r for r in reversed(reviews)
                      if r.kind == ReviewKind.HUMAN.value), None)
        if auto and human:
            yield sub, auto, human, project.track


def _bucket() -> dict:
    return {"pairs": 0, "agree": 0, "false_pass": 0, "false_hold": 0,
            "score_pairs": 0, "score_exact": 0, "score_adjacent": 0}


def _rates(b: dict) -> dict:
    n = b["pairs"]
    out = {
        "pairs": n,
        "verdict_agreement_rate": round(b["agree"] / n, 3) if n else None,
        "false_pass_rate": round(b["false_pass"] / n, 3) if n else None,
        "false_hold_rate": round(b["false_hold"] / n, 3) if n else None,
    }
    sn = b["score_pairs"]
    out["score_exact_agreement_rate"] = round(b["score_exact"] / sn, 3) if sn else None
    out["score_adjacent_agreement_rate"] = round(b["score_adjacent"] / sn, 3) if sn else None
    return out


def calibration_report(db: Session, track: str | None = None) -> dict:
    overall = _bucket()
    by_track: dict[str, dict] = defaultdict(_bucket)
    by_grader: dict[str, dict] = defaultdict(_bucket)

    for sub, auto, human, sub_track in _pairs(db, track):
        auto_pass = auto.verdict == Verdict.PASS.value
        human_pass = human.verdict == Verdict.PASS.value
        grader = (auto.detail or {}).get("grader_version") or "unversioned"

        for b in (overall, by_track[sub_track], by_grader[grader]):
            b["pairs"] += 1
            if auto_pass == human_pass:
                b["agree"] += 1
            elif auto_pass and not human_pass:
                b["false_pass"] += 1
            else:
                b["false_hold"] += 1
            if auto.overall_score is not None and human.overall_score is not None:
                diff = abs(round(auto.overall_score) - round(human.overall_score))
                b["score_pairs"] += 1
                if diff == 0:
                    b["score_exact"] += 1
                if diff <= 1:
                    b["score_adjacent"] += 1

    rates = _rates(overall)
    n = rates["pairs"]
    fp = rates["false_pass_rate"]
    agree = rates["verdict_agreement_rate"]
    if n == 0:
        recommendation, reason = "hold", "no calibration pairs yet"
    elif fp is not None and fp > FALSE_PASS_TOLERANCE:
        recommendation = "raise_sampling"
        reason = (f"false-pass rate {fp:.1%} exceeds tolerance "
                  f"{FALSE_PASS_TOLERANCE:.0%} — widen human review")
    elif n < MIN_PAIRS_FOR_CONFIDENCE:
        recommendation, reason = "hold", f"only {n} pairs (< {MIN_PAIRS_FOR_CONFIDENCE})"
    elif agree is not None and agree >= STRONG_AGREEMENT:
        recommendation = "consider_lower"
        reason = (f"agreement {agree:.1%} on {n} pairs — current sampling floors "
                  "are defensible; review before changing")
    else:
        recommendation, reason = "hold", "agreement below the strong threshold"

    return {
        "track_filter": track,
        **rates,
        "by_track": {k: _rates(v) for k, v in sorted(by_track.items())},
        "by_grader_version": {k: _rates(v) for k, v in sorted(by_grader.items())},
        "thresholds": {
            "false_pass_tolerance": FALSE_PASS_TOLERANCE,
            "min_pairs_for_confidence": MIN_PAIRS_FOR_CONFIDENCE,
            "strong_agreement": STRONG_AGREEMENT,
        },
        "sampling_recommendation": recommendation,
        "recommendation_reason": reason,
    }
