import pytest

from factory import controls
from factory.config import settings
from factory.models import DomainTrack, Expert, ExpertStatus, Submission


def test_unqualified_expert_blocked(db):
    ex = Expert(name="New", email="new@x.test", status=ExpertStatus.PENDING.value)
    db.add(ex)
    db.flush()
    with pytest.raises(controls.ControlViolation) as e:
        controls.assert_qualified(db, ex, DomainTrack.FINANCIAL_ACCOUNTING.value)
    assert e.value.control == "C1"


def test_qualified_expert_wrong_track_blocked(db, qualified_expert):
    with pytest.raises(controls.ControlViolation):
        controls.assert_qualified(db, qualified_expert, DomainTrack.TAX.value)


def test_qualified_expert_passes(db, qualified_expert):
    controls.assert_qualified(db, qualified_expert, DomainTrack.FINANCIAL_ACCOUNTING.value)


def test_author_cannot_self_review(db, qualified_expert):
    sub = Submission(task_id="t", expert_id=qualified_expert.id, content={})
    qualified_expert.is_reviewer = True
    with pytest.raises(controls.ControlViolation) as e:
        controls.assert_reviewer_independent(sub, qualified_expert)
    assert e.value.control == "C2"


def test_non_reviewer_cannot_review(db, qualified_expert, reviewer):
    sub = Submission(task_id="t", expert_id=reviewer.id, content={})
    with pytest.raises(controls.ControlViolation):
        controls.assert_reviewer_independent(sub, qualified_expert)  # not a reviewer


def test_reviewer_independence_ok(db, qualified_expert, reviewer):
    sub = Submission(task_id="t", expert_id=qualified_expert.id, content={})
    controls.assert_reviewer_independent(sub, reviewer)


def test_sampling_rates_by_tier(db):
    new = Expert(name="n", email="n@x.test", approved_count=0)
    assert controls.sampling_rate(new) == settings.sampling_new

    standard = Expert(name="s", email="s@x.test", approved_count=10, quality_score=3.8)
    assert controls.sampling_rate(standard) == settings.sampling_standard

    trusted = Expert(name="t", email="t@x.test", approved_count=30, quality_score=4.6)
    assert controls.sampling_rate(trusted) == settings.sampling_trusted


def test_sampling_is_deterministic(db):
    ex = Expert(name="s", email="s2@x.test", approved_count=10, quality_score=3.8)
    first = controls.is_sampled_for_human_review("submission-abc", ex)
    assert all(controls.is_sampled_for_human_review("submission-abc", ex) == first
               for _ in range(10))


def test_sampling_rate_approximates_config(db):
    ex = Expert(name="s", email="s3@x.test", approved_count=10, quality_score=3.8)
    n = 5000
    hits = sum(controls.is_sampled_for_human_review(f"sub-{i}", ex) for i in range(n))
    assert abs(hits / n - settings.sampling_standard) < 0.03


def test_gold_fail_penalizes_and_suspends(db, qualified_expert):
    start = qualified_expert.quality_score
    controls.register_gold_result(db, qualified_expert, passed=False)
    assert qualified_expert.quality_score < start
    controls.register_gold_result(db, qualified_expert, passed=False)
    controls.register_gold_result(db, qualified_expert, passed=False)
    assert qualified_expert.status == ExpertStatus.SUSPENDED.value


def test_low_score_suspends(db, qualified_expert):
    controls.update_quality_score(db, qualified_expert, 0.5)
    controls.update_quality_score(db, qualified_expert, 0.5)
    controls.update_quality_score(db, qualified_expert, 0.5)
    controls.update_quality_score(db, qualified_expert, 0.5)
    assert qualified_expert.status == ExpertStatus.SUSPENDED.value


def test_export_dual_control(db):
    with pytest.raises(controls.ControlViolation) as e:
        controls.assert_export_dual_control("ops", "ops")
    assert e.value.control == "C6"
    controls.assert_export_dual_control("ops", "reviewer")


def test_gold_answer_matching():
    gold = {"must_include": ["short-term", "12 months"], "must_not_include": ["finance lease"]}
    good = {"response": "The short-term exemption applies to terms of 12 months or less."}
    bad_missing = {"response": "This is a finance lease situation."}
    assert controls.gold_answer_matches(good, gold, "sft")
    assert not controls.gold_answer_matches(bad_missing, gold, "sft")
