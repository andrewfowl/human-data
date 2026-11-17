from datetime import datetime, timedelta, timezone

import pytest

from factory import audit, db as database, jobs
from factory.models import AuditEvent, Job, JobStatus

CALLS: list[dict] = []


@jobs.handler("test.ok")
def _ok(db, payload):
    CALLS.append(payload)
    return {"echo": payload.get("n")}


@jobs.handler("test.flaky")
def _flaky(db, payload):
    CALLS.append(payload)
    if len(CALLS) < payload["fail_times"] + 1:
        raise RuntimeError("transient")
    return {"ok": True}


@jobs.handler("test.permanent")
def _permanent(db, payload):
    raise jobs.PermanentError("bad input")


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()


def _past(job, db):
    job.run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()


def test_enqueue_commits_with_caller_and_runs(db):
    job, created = jobs.enqueue(db, "test.ok", {"n": 1})
    db.commit()
    assert created and job.status == JobStatus.QUEUED.value
    done = jobs.run_inline(db, job.id)
    assert done.status == JobStatus.SUCCEEDED.value
    assert done.result == {"echo": 1} and done.attempts == 1


def test_enqueue_is_rolled_back_with_caller(db):
    jobs.enqueue(db, "test.ok", {"n": 2})
    db.rollback()
    assert db.query(Job).count() == 0


def test_retry_with_backoff_then_success(db):
    job, _ = jobs.enqueue(db, "test.flaky", {"fail_times": 1})
    db.commit()
    first = jobs.run_inline(db, job.id)
    assert first.status == JobStatus.QUEUED.value
    assert first.attempts == 1 and "transient" in first.last_error
    run_at = first.run_at if first.run_at.tzinfo else first.run_at.replace(tzinfo=timezone.utc)
    assert run_at > datetime.now(timezone.utc)  # backoff scheduled in the future
    assert jobs.claim(db) == []                  # not runnable until then

    _past(first, db)
    second = jobs.run_inline(db, job.id)
    assert second.status == JobStatus.SUCCEEDED.value and second.attempts == 2


def test_dead_letter_after_max_attempts_is_audited_and_requeueable(db):
    job, _ = jobs.enqueue(db, "test.flaky", {"fail_times": 99}, max_attempts=2)
    db.commit()
    j = jobs.run_inline(db, job.id)
    _past(j, db)
    j = jobs.run_inline(db, job.id)
    assert j.status == JobStatus.DEAD.value and j.attempts == 2
    assert db.query(AuditEvent).filter_by(action="job.dead_lettered", entity_id=job.id).count() == 1

    j = jobs.requeue(db, j, actor="ops")
    assert j.status == JobStatus.QUEUED.value and j.attempts == 0
    assert audit.verify_chain(db)[0]


def test_permanent_error_skips_retries(db):
    job, _ = jobs.enqueue(db, "test.permanent", {})
    db.commit()
    j = jobs.run_inline(db, job.id)
    assert j.status == JobStatus.DEAD.value and j.attempts == 1


def test_unknown_kind_dead_letters(db):
    job, _ = jobs.enqueue(db, "test.nope", {})
    db.commit()
    assert jobs.run_inline(db, job.id).status == JobStatus.DEAD.value


def test_dedupe_key_returns_existing_job(db):
    a, created_a = jobs.enqueue(db, "test.ok", {"n": 1}, dedupe_key="evt_1")
    db.commit()
    b, created_b = jobs.enqueue(db, "test.ok", {"n": 1}, dedupe_key="evt_1")
    assert created_a and not created_b and a.id == b.id
    assert db.query(Job).count() == 1


def test_job_cannot_be_claimed_twice(db):
    job, _ = jobs.enqueue(db, "test.ok", {})
    db.commit()
    assert len(jobs.claim(db, worker_id="w1")) == 1
    with database.session() as other:
        assert jobs.claim(other, worker_id="w2") == []


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(db):
    job, _ = jobs.enqueue(db, "test.ok", {"n": 7})
    db.commit()
    [stale] = jobs.claim(db, worker_id="crashed")
    stale_token = stale.attempts
    stale.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with database.session() as other:
        [fresh] = jobs.claim(other, worker_id="w2")
        assert fresh.attempts == stale_token + 1
        # the crashed worker wakes up and tries to report: its token is stale
        assert jobs._settle(other, job.id, stale_token, status=JobStatus.DEAD.value) is False
        assert jobs.execute(other, fresh).status == JobStatus.SUCCEEDED.value


def test_work_drains_queue(db):
    for n in range(3):
        jobs.enqueue(db, "test.ok", {"n": n})
    db.commit()
    stats = jobs.work(database.session, max_jobs=10)
    assert stats == {"processed": 3, "succeeded": 3, "retrying": 0, "dead": 0}
    assert jobs.queue_stats(db)["by_status"]["succeeded"] == 3


def test_backoff_is_capped_and_jittered():
    for attempt in range(1, 30):
        s = jobs.backoff_seconds(attempt)
        assert 0 < s <= jobs.settings.job_backoff_cap_seconds
