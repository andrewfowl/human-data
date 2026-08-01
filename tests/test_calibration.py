"""Calibration reporting, grader versioning, and evidence-bound review schema."""

from unittest.mock import patch

from factory.models import Review, ReviewKind, Submission, SubmissionStatus, Verdict
from factory.qc import auto_reviewer, calibration, engine
from factory.rubrics import get_rubric
from tests.conftest import GOOD_SFT


def _pair(db, task, expert, auto_verdict, human_verdict, auto_score=4.5,
          human_score=4.0, grader="qc-prompt-v2/fin-sft-v1/claude-opus-4-8"):
    sub = Submission(task_id=task.id, expert_id=expert.id, content=GOOD_SFT,
                     status=SubmissionStatus.APPROVED.value)
    db.add(sub)
    db.flush()
    db.add(Review(submission_id=sub.id, kind=ReviewKind.AUTO_LLM.value,
                  reviewer_id=auto_reviewer.AUTO_REVIEWER_ID, verdict=auto_verdict,
                  overall_score=auto_score, detail={"grader_version": grader}))
    db.add(Review(submission_id=sub.id, kind=ReviewKind.HUMAN.value,
                  reviewer_id="rev-1", verdict=human_verdict,
                  overall_score=human_score, detail={}))
    db.commit()
    return sub


def test_calibration_counts_agreement_and_false_pass(db, project, task, qualified_expert):
    _pair(db, task, qualified_expert, Verdict.PASS.value, Verdict.PASS.value)          # agree
    _pair(db, task, qualified_expert, Verdict.PASS.value, Verdict.REVISE.value)        # false pass
    _pair(db, task, qualified_expert, Verdict.REVISE.value, Verdict.PASS.value)        # false hold
    _pair(db, task, qualified_expert, Verdict.REVISE.value, Verdict.FAIL.value)        # agree

    r = calibration.calibration_report(db)
    assert r["pairs"] == 4
    assert r["verdict_agreement_rate"] == 0.5
    assert r["false_pass_rate"] == 0.25
    assert r["false_hold_rate"] == 0.25
    # false-pass 25% >> 5% tolerance → must recommend widening human review
    assert r["sampling_recommendation"] == "raise_sampling"
    assert "financial_accounting" in r["by_track"]
    assert "qc-prompt-v2/fin-sft-v1/claude-opus-4-8" in r["by_grader_version"]


def test_calibration_score_agreement(db, project, task, qualified_expert):
    _pair(db, task, qualified_expert, Verdict.PASS.value, Verdict.PASS.value,
          auto_score=4.2, human_score=4.4)   # exact after rounding (4 vs 4)
    _pair(db, task, qualified_expert, Verdict.PASS.value, Verdict.PASS.value,
          auto_score=5.0, human_score=4.0)   # adjacent
    _pair(db, task, qualified_expert, Verdict.PASS.value, Verdict.PASS.value,
          auto_score=5.0, human_score=2.0)   # neither
    r = calibration.calibration_report(db)
    assert r["score_exact_agreement_rate"] == round(1 / 3, 3)
    assert r["score_adjacent_agreement_rate"] == round(2 / 3, 3)


def test_calibration_empty_holds(db):
    r = calibration.calibration_report(db)
    assert r["pairs"] == 0
    assert r["sampling_recommendation"] == "hold"


def test_review_schema_requires_evidence():
    schema = auto_reviewer._review_schema(get_rubric("fin-sft-v1"))
    item = schema["properties"]["criterion_scores"]["items"]
    assert "evidence" in item["properties"]
    assert "evidence" in item["required"]
    assert "insufficient_evidence" in schema["required"]


def test_offline_fallback_marks_insufficient_evidence_and_version(db, task, qualified_expert):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    auto = next(r for r in sub.reviews if r.kind == ReviewKind.AUTO_LLM.value)
    assert auto.detail["insufficient_evidence"] is True
    assert auto.detail["grader_version"] == "qc-prompt-v2/fin-sft-v1/heuristic-v1"


def test_live_review_with_insufficient_evidence_never_auto_approves(db, task, qualified_expert):
    rubric = get_rubric("fin-sft-v1")
    crafted = auto_reviewer.AutoReview(
        reviewer_id=auto_reviewer.AUTO_REVIEWER_ID,
        overall_score=4.8,          # well above the pass threshold...
        passed=False,               # ...but ungrounded, so the reviewer holds it
        criterion_scores=[{"criterion": c.key, "score": 5, "justification": "x",
                           "evidence": ""} for c in rubric.criteria],
        flags=[],
        summary="high score without quotable grounding",
        insufficient_evidence=True,
        grader_version=auto_reviewer.grader_version(rubric, live=True),
    )
    with patch.object(engine.auto_reviewer, "review", return_value=crafted):
        sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    assert sub.status == SubmissionStatus.HUMAN_REVIEW.value
