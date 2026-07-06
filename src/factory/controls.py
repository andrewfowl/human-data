"""Internal control policies embedded in the workflow (control gate library).

Controls implemented here:
  C1  Qualification gating      — only track-qualified experts receive tasks
  C2  Segregation of duties     — author can never review their own work;
                                  project creator cannot be the sole export approver
  C3  Adaptive review sampling  — human-review rate scales inversely with the
                                  contributor's demonstrated quality (with a floor)
  C4  Gold-task calibration     — honeypot tasks scored against a hidden answer;
                                  misses penalize the contributor's trust score
  C5  Contributor scoring       — EWMA quality score drives sampling and suspension
  C6  Dual-control exports      — a release requires a second approver and a
                                  clean control report

Sampling decisions are derived deterministically from the submission id so the
decision is reproducible in an audit (no unlogged randomness in the pipeline).
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Expert, ExpertStatus, Qualification, Submission

SUSPEND_BELOW = 2.0
GOLD_SUSPEND_FAILS = 3


class ControlViolation(Exception):
    """Raised when an action would violate an internal control."""

    def __init__(self, control: str, message: str):
        self.control = control
        super().__init__(f"[{control}] {message}")


# C1 — qualification gating -----------------------------------------------------

def assert_qualified(db: Session, expert: Expert, track: str) -> None:
    if expert.status != ExpertStatus.QUALIFIED.value:
        raise ControlViolation("C1", f"expert {expert.id} is not in QUALIFIED status")
    q = db.execute(
        select(Qualification).where(
            Qualification.expert_id == expert.id,
            Qualification.track == track,
            Qualification.passed.is_(True),
        )
    ).scalar_one_or_none()
    if q is None:
        raise ControlViolation("C1", f"expert {expert.id} holds no passed qualification for track '{track}'")


# C2 — segregation of duties ----------------------------------------------------

def assert_reviewer_independent(submission: Submission, reviewer: Expert) -> None:
    if reviewer.id == submission.expert_id:
        raise ControlViolation("C2", "author cannot review their own submission")
    if not reviewer.is_reviewer:
        raise ControlViolation("C2", f"expert {reviewer.id} does not hold the reviewer role")
    if reviewer.status != ExpertStatus.QUALIFIED.value:
        raise ControlViolation("C2", "reviewer must be in QUALIFIED status")


def assert_export_dual_control(requested_by: str, approved_by: str) -> None:
    if requested_by == approved_by:
        raise ControlViolation("C6", "export approver must differ from the requester (dual control)")


# C3 — adaptive human-review sampling --------------------------------------------

def sampling_rate(expert: Expert) -> float:
    if expert.approved_count < 5:
        return settings.sampling_new
    if (expert.quality_score >= settings.trusted_score
            and expert.approved_count >= settings.trusted_min_approved):
        return settings.sampling_trusted
    return settings.sampling_standard


def is_sampled_for_human_review(submission_id: str, expert: Expert) -> bool:
    """Deterministic, auditable sampling: hash the submission id into [0,1)."""
    rate = sampling_rate(expert)
    bucket = int(hashlib.sha256(submission_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


# C4 / C5 — gold calibration and contributor scoring ------------------------------

def update_quality_score(db: Session, expert: Expert, review_score: float) -> float:
    a = settings.score_ewma_alpha
    expert.quality_score = round(a * review_score + (1 - a) * expert.quality_score, 4)
    _maybe_suspend(expert)
    db.flush()
    return expert.quality_score


def register_gold_result(db: Session, expert: Expert, passed: bool) -> None:
    if passed:
        expert.gold_pass_count += 1
    else:
        expert.gold_fail_count += 1
        expert.quality_score = round(expert.quality_score * settings.gold_fail_penalty, 4)
    _maybe_suspend(expert)
    db.flush()


def _maybe_suspend(expert: Expert) -> None:
    if expert.quality_score < SUSPEND_BELOW or expert.gold_fail_count >= GOLD_SUSPEND_FAILS:
        expert.status = ExpertStatus.SUSPENDED.value


def gold_answer_matches(submission_content: dict, gold_answer: dict, task_type: str) -> bool:
    """Keyword-coverage check of the submission against the hidden gold answer.

    The gold answer carries `must_include` (list of concept keywords) and an
    optional `must_not_include`. All must-include terms must appear; any
    must-not term appearing is a fail.
    """
    main_key = {"sft": "response", "preference": "chosen", "eval": "answer"}.get(task_type, "response")
    text = str(submission_content.get(main_key, "")).lower()
    for term in gold_answer.get("must_include", []):
        if term.lower() not in text:
            return False
    for term in gold_answer.get("must_not_include", []):
        if term.lower() in text:
            return False
    return True
