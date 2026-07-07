"""Workflow engine: the QC pipeline state machine.

Submission lifecycle:

    SUBMITTED
      └─ gate 1: deterministic validators ──fail──▶ AUTO_CHECK_FAILED (revise & resubmit)
      └─ gate 2: gold check (honeypot tasks) — scores the contributor, gold tasks
                 are calibration-only and never enter the dataset
      └─ gate 3: autonomous LLM QC review
             score < gray threshold or critical flag ──▶ NEEDS_REVISION
             gray zone, offline fallback, or sampled (C3) ──▶ HUMAN_REVIEW
             pass and not sampled ──▶ APPROVED  (auto-approval, fully recorded)
    HUMAN_REVIEW ──reviewer verdict──▶ APPROVED / NEEDS_REVISION / REJECTED

Every transition writes a hash-chained audit event.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, controls, validators
from ..billing import service as billing
from ..config import settings
from ..models import (
    Expert, Project, Review, ReviewKind, Submission, SubmissionStatus, Task,
    TaskStatus, Verdict,
)
from ..rubrics import get_rubric
from . import auto_reviewer

ENGINE_ACTOR = "qc-engine"


class PipelineError(Exception):
    pass


def _project_fingerprints(db: Session, project_id: str, task_type: str,
                          exclude_submission: str) -> set[str]:
    rows = db.execute(
        select(Submission)
        .join(Task, Submission.task_id == Task.id)
        .where(Task.project_id == project_id, Submission.id != exclude_submission)
    ).scalars().all()
    return {validators.content_fingerprint(s.content, task_type) for s in rows}


def submit(db: Session, *, task: Task, expert: Expert, content: dict) -> Submission:
    """Create a submission and run it through the automated QC gates."""
    project: Project = db.get(Project, task.project_id)
    controls.assert_qualified(db, expert, project.track)
    if task.assigned_to and task.assigned_to != expert.id:
        raise PipelineError("task is assigned to a different expert")

    prior = db.execute(
        select(Submission).where(Submission.task_id == task.id,
                                 Submission.expert_id == expert.id)
        .order_by(Submission.version.desc())
    ).scalars().first()
    version = (prior.version + 1) if prior else 1

    sub = Submission(task_id=task.id, expert_id=expert.id, content=content, version=version)
    db.add(sub)
    db.flush()
    audit.record(db, actor=expert.id, action="submission.created", entity_type="submission",
                 entity_id=sub.id, payload={"task_id": task.id, "version": version})

    process(db, sub)
    return sub


def process(db: Session, sub: Submission) -> Submission:
    """Run gates 1–3 on a freshly submitted (or resubmitted) submission."""
    task: Task = db.get(Task, sub.task_id)
    project: Project = db.get(Project, task.project_id)
    expert: Expert = db.get(Expert, sub.expert_id)
    rubric = get_rubric(project.rubric_id)

    # Gate 1 — deterministic validators
    fingerprints = _project_fingerprints(db, project.id, project.task_type, sub.id)
    validation = validators.run_all(sub.content, project.task_type, fingerprints)
    db.add(Review(
        submission_id=sub.id, kind=ReviewKind.DETERMINISTIC.value,
        reviewer_id="validators-v1",
        verdict=(Verdict.PASS if validation.passed else Verdict.FAIL).value,
        detail=validation.as_dict(),
    ))
    if not validation.passed:
        sub.status = SubmissionStatus.AUTO_CHECK_FAILED.value
        audit.record(db, actor=ENGINE_ACTOR, action="submission.auto_check_failed",
                     entity_type="submission", entity_id=sub.id,
                     payload={"errors": [f.code for f in validation.errors]})
        db.commit()
        return sub

    # Gate 2 — gold-task calibration (C4). Gold tasks never enter the dataset.
    if task.is_gold and task.gold_answer:
        passed = controls.gold_answer_matches(sub.content, task.gold_answer, project.task_type)
        controls.register_gold_result(db, expert, passed)
        db.add(Review(
            submission_id=sub.id, kind=ReviewKind.GOLD_CHECK.value, reviewer_id=ENGINE_ACTOR,
            verdict=(Verdict.PASS if passed else Verdict.FAIL).value,
            detail={"gold": True},
        ))
        sub.status = (SubmissionStatus.APPROVED if passed
                      else SubmissionStatus.REJECTED).value
        task.status = TaskStatus.COMPLETED.value
        audit.record(db, actor=ENGINE_ACTOR, action="submission.gold_checked",
                     entity_type="submission", entity_id=sub.id,
                     payload={"passed": passed, "expert_score": expert.quality_score,
                              "expert_status": expert.status})
        db.commit()
        return sub

    # Gate 3 — autonomous LLM QC review
    sub.status = SubmissionStatus.AUTO_QC_PENDING.value
    result = auto_reviewer.review(task.prompt, task.context, project.task_type,
                                  sub.content, rubric, validation)
    db.add(Review(
        submission_id=sub.id, kind=ReviewKind.AUTO_LLM.value, reviewer_id=result.reviewer_id,
        verdict=(Verdict.PASS if result.passed else Verdict.REVISE).value,
        overall_score=result.overall_score, detail=result.as_detail(),
    ))

    critical = set(result.flags) & auto_reviewer.CRITICAL_FLAGS
    offline = auto_reviewer.FALLBACK_REVIEWER_ID == result.reviewer_id
    sampled = controls.is_sampled_for_human_review(sub.id, expert)
    sub.sampled_for_human_review = sampled

    if critical or result.overall_score < settings.qc_gray_threshold:
        sub.status = SubmissionStatus.NEEDS_REVISION.value
        outcome = "needs_revision"
    elif offline or sampled or not result.passed:
        # Gray zone, offline fallback, or C3 sampling — a human reviewer decides.
        sub.status = SubmissionStatus.HUMAN_REVIEW.value
        outcome = "human_review"
    else:
        sub.status = SubmissionStatus.APPROVED.value
        task.status = TaskStatus.COMPLETED.value
        expert.approved_count += 1
        controls.update_quality_score(db, expert, result.overall_score)
        billing.record_approved_record(db, project=project, task=task, submission=sub)
        outcome = "auto_approved"

    audit.record(db, actor=result.reviewer_id, action=f"submission.{outcome}",
                 entity_type="submission", entity_id=sub.id,
                 payload={"score": result.overall_score, "flags": result.flags,
                          "sampled": sampled})
    db.commit()
    return sub


def human_review(db: Session, *, submission: Submission, reviewer: Expert,
                 verdict: str, criterion_scores: list[dict] | None = None,
                 comments: str = "") -> Review:
    """Record a human review verdict (gate 4). Enforces C2 segregation of duties."""
    if submission.status != SubmissionStatus.HUMAN_REVIEW.value:
        raise PipelineError(f"submission is in status '{submission.status}', not human_review")
    controls.assert_reviewer_independent(submission, reviewer)
    if verdict not in (Verdict.PASS.value, Verdict.REVISE.value, Verdict.FAIL.value):
        raise PipelineError(f"invalid verdict '{verdict}'")

    task: Task = db.get(Task, submission.task_id)
    project: Project = db.get(Project, task.project_id)
    rubric = get_rubric(project.rubric_id)
    author: Expert = db.get(Expert, submission.expert_id)

    overall = None
    if criterion_scores:
        overall = auto_reviewer._weighted_overall(rubric, criterion_scores)

    rev = Review(
        submission_id=submission.id, kind=ReviewKind.HUMAN.value, reviewer_id=reviewer.id,
        verdict=verdict, overall_score=overall,
        detail={"criterion_scores": criterion_scores or [], "comments": comments},
    )
    db.add(rev)

    if verdict == Verdict.PASS.value:
        submission.status = SubmissionStatus.APPROVED.value
        task.status = TaskStatus.COMPLETED.value
        author.approved_count += 1
        controls.update_quality_score(db, author, overall if overall is not None else 4.0)
        billing.record_approved_record(db, project=project, task=task, submission=submission)
    elif verdict == Verdict.REVISE.value:
        submission.status = SubmissionStatus.NEEDS_REVISION.value
    else:
        submission.status = SubmissionStatus.REJECTED.value
        controls.update_quality_score(db, author, overall if overall is not None else 1.0)

    audit.record(db, actor=reviewer.id, action=f"submission.human_{verdict}",
                 entity_type="submission", entity_id=submission.id,
                 payload={"overall_score": overall, "comments": comments[:500]})
    db.commit()
    return rev
