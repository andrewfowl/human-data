import pytest

from factory import audit
from factory.models import (
    Expert, ExpertStatus, Review, ReviewKind, Submission, SubmissionStatus, Task,
)
from factory.qc import engine
from factory.qc.auto_reviewer import FALLBACK_REVIEWER_ID
from tests.conftest import GOOD_SFT


def test_hard_validation_failure_blocks(db, task, qualified_expert):
    bad = {"response": "Client SSN is 123-45-6789. " + "x" * 300, "citations": ["ASC 606"]}
    sub = engine.submit(db, task=task, expert=qualified_expert, content=bad)
    assert sub.status == SubmissionStatus.AUTO_CHECK_FAILED.value
    det = [r for r in sub.reviews if r.kind == ReviewKind.DETERMINISTIC.value]
    assert det and det[0].verdict == "fail"


def test_offline_review_routes_to_human_never_auto_approves(db, task, qualified_expert):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    # Offline fallback: passed=False → must land in human_review or needs_revision,
    # never auto-approved.
    assert sub.status in (SubmissionStatus.HUMAN_REVIEW.value,
                          SubmissionStatus.NEEDS_REVISION.value)
    auto = [r for r in sub.reviews if r.kind == ReviewKind.AUTO_LLM.value]
    assert auto[0].reviewer_id == FALLBACK_REVIEWER_ID
    assert "OFFLINE_FALLBACK" in auto[0].detail["flags"]


def test_unqualified_expert_cannot_submit(db, task):
    stranger = Expert(name="X", email="x@x.test", status=ExpertStatus.QUALIFIED.value)
    db.add(stranger)
    db.commit()
    from factory.controls import ControlViolation
    with pytest.raises(ControlViolation):
        engine.submit(db, task=task, expert=stranger, content=GOOD_SFT)


def test_human_review_approves(db, task, qualified_expert, reviewer):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    assert sub.status == SubmissionStatus.HUMAN_REVIEW.value
    before = qualified_expert.approved_count
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass",
                        comments="Accurate and complete.")
    assert sub.status == SubmissionStatus.APPROVED.value
    assert qualified_expert.approved_count == before + 1


def test_human_review_self_review_blocked(db, task, qualified_expert):
    qualified_expert.is_reviewer = True
    db.commit()
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    from factory.controls import ControlViolation
    with pytest.raises(ControlViolation):
        engine.human_review(db, submission=sub, reviewer=qualified_expert, verdict="pass")


def test_human_review_revise_and_resubmit(db, task, qualified_expert, reviewer):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="revise",
                        comments="Add the residual approach discussion.")
    assert sub.status == SubmissionStatus.NEEDS_REVISION.value

    revised = dict(GOOD_SFT)
    revised["response"] = GOOD_SFT["response"] + (
        " The residual approach is only permitted when the SSP is highly variable "
        "or uncertain, per ASC 606."
    )
    sub2 = engine.submit(db, task=task, expert=qualified_expert, content=revised)
    assert sub2.version == 2


def test_gold_task_pass_and_fail(db, project, qualified_expert):
    gold = Task(project_id=project.id, prompt="Short-term lease question", is_gold=True,
                gold_answer={"must_include": ["short-term", "12 months"]})
    db.add(gold)
    db.commit()

    good = {
        "response": "The short-term lease exemption applies because the term is 12 months "
                    "or less at commencement; the lessee recognizes straight-line expense "
                    "and no right-of-use asset. " + "Detail. " * 20,
        "citations": ["ASC 842"],
    }
    sub = engine.submit(db, task=gold, expert=qualified_expert, content=good)
    assert sub.status == SubmissionStatus.APPROVED.value
    assert qualified_expert.gold_pass_count == 1

    gold2 = Task(project_id=project.id, prompt="Gold 2", is_gold=True,
                 gold_answer={"must_include": ["impairment"]})
    db.add(gold2)
    db.commit()
    miss = {"response": "This response never mentions the required concept at all. " * 10,
            "citations": ["ASC 842"]}
    score_before = qualified_expert.quality_score
    sub2 = engine.submit(db, task=gold2, expert=qualified_expert, content=miss)
    assert sub2.status == SubmissionStatus.REJECTED.value
    assert qualified_expert.gold_fail_count == 1
    assert qualified_expert.quality_score < score_before


def test_every_transition_is_audited_and_chain_verifies(db, task, qualified_expert, reviewer):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass")
    ok, n = audit.verify_chain(db)
    assert ok and n >= 3  # created + routed + human verdict


def test_tampering_breaks_audit_chain(db, task, qualified_expert):
    engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    from factory.models import AuditEvent
    ev = db.query(AuditEvent).first()
    ev.action = "something.else"
    db.commit()
    ok, _ = audit.verify_chain(db)
    assert not ok
