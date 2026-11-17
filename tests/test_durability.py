"""QC and Stripe webhooks running through the durable queue."""

import json

import pytest
from fastapi.testclient import TestClient

from factory import db as database, jobs
from factory.api.main import app
from factory.config import settings
from factory.models import Job, JobStatus, SubmissionStatus
from factory.qc import engine
from tests.conftest import GOOD_SFT


def test_submit_runs_qc_inline_via_job(db, task, qualified_expert):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    assert sub.status != SubmissionStatus.SUBMITTED.value
    [job] = db.query(Job).all()
    assert job.kind == engine.QC_JOB and job.status == JobStatus.SUCCEEDED.value
    assert job.payload == {"submission_id": sub.id}


def test_qc_failure_leaves_retryable_job_not_lost_submission(db, task, qualified_expert,
                                                             monkeypatch):
    real = engine.process
    calls = {"n": 0}

    def flaky(db_, sub):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM provider timeout")
        return real(db_, sub)

    monkeypatch.setattr(engine, "process", flaky)
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    # The submission is committed and the request did not fail…
    assert sub.status == SubmissionStatus.SUBMITTED.value
    [job] = db.query(Job).all()
    assert job.status == JobStatus.QUEUED.value and "LLM provider timeout" in job.last_error

    # …and a later sweep finishes the review.
    job.run_at = job.created_at
    db.commit()
    stats = jobs.work(database.session)
    assert stats["succeeded"] == 1
    db.refresh(sub)
    assert sub.status != SubmissionStatus.SUBMITTED.value


def test_qc_job_redelivery_is_a_noop(db, task, qualified_expert):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    n_reviews = len(sub.reviews) if hasattr(sub, "reviews") else None
    result = engine._qc_job(db, {"submission_id": sub.id})
    assert result["skipped"] is True
    if n_reviews is not None:
        db.refresh(sub)
        assert len(sub.reviews) == n_reviews


def test_deferred_mode_only_enqueues(db, task, qualified_expert, monkeypatch):
    monkeypatch.setattr(settings, "queue_mode", "deferred")
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    assert sub.status == SubmissionStatus.SUBMITTED.value
    assert db.query(Job).one().status == JobStatus.QUEUED.value
    jobs.work(database.session)
    db.refresh(sub)
    assert sub.status != SubmissionStatus.SUBMITTED.value


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def _post_event(client, event):
    return client.post("/billing/stripe/webhook", content=json.dumps(event),
                       headers={"Content-Type": "application/json"})


def test_webhook_event_is_deduped_on_stripe_event_id(client, db):
    event = {"id": "evt_dup_1", "type": "customer.created", "data": {"object": {}}}
    r1 = _post_event(client, event)
    r2 = _post_event(client, event)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["duplicate"] is False and r2.json()["duplicate"] is True
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert db.query(Job).filter_by(dedupe_key="stripe:evt_dup_1").count() == 1


def test_webhook_handler_failure_still_acknowledges(client, db, monkeypatch):
    from factory.billing import service as billing

    def boom(db_, event):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(billing, "handle_stripe_event", boom)
    r = _post_event(client, {"id": "evt_fail_1", "type": "invoice.paid",
                             "data": {"object": {"id": "in_1"}}})
    assert r.status_code == 200
    assert r.json()["accepted"] is True and r.json()["status"] == JobStatus.QUEUED.value


def test_jobs_endpoint_and_requeue(client, db):
    jobs.enqueue(db, "test.unknown-kind", {})
    db.commit()
    jobs.work(database.session)
    body = client.get("/jobs?status=dead", headers={"X-Actor-Id": "ops-admin"}).json()
    [dead] = body["jobs"]
    assert body["stats"]["by_status"]["dead"] == 1
    r = client.post(f"/jobs/{dead['id']}/requeue", headers={"X-Actor-Id": "ops-admin"})
    assert r.status_code == 200 and r.json()["status"] == JobStatus.QUEUED.value
    r = client.post(f"/jobs/{dead['id']}/requeue", headers={"X-Actor-Id": "ops-admin"})
    assert r.status_code == 409


def test_cron_requires_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "")
    assert client.get("/cron/jobs").status_code == 503
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    assert client.get("/cron/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/cron/jobs", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and "processed" in r.json()
