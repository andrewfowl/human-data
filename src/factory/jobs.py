"""Durable background jobs on the primary database.

Patterns:
  * Transactional outbox — `enqueue` adds the job to the caller's session, so it
    commits atomically with the business change (no "row saved, job lost").
  * Lease-based claiming — `FOR UPDATE SKIP LOCKED` on Postgres plus a
    conditional UPDATE (works on SQLite too) so two workers never take the same
    job. A worker that dies mid-job loses its lease and the job is reclaimed.
  * Fencing — the attempt number claimed is the token; a stale worker finishing
    after its job was reclaimed cannot overwrite the newer attempt's outcome.
  * Retries with capped exponential backoff and jitter, then a dead-letter state
    (audited) that an operator can inspect and requeue.

Delivery is at-least-once: handlers MUST be idempotent (check current state
before acting). Register handlers with `@handler("kind")`.
"""

from __future__ import annotations

import os
import random
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit
from .config import settings
from .models import Job, JobStatus

Handler = Callable[[Session, dict], "dict | None"]
_HANDLERS: dict[str, Handler] = {}

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class PermanentError(Exception):
    """Raise from a handler when retrying cannot help; the job dead-letters at once."""


def handler(kind: str):
    def register(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn
    return register


def load_handlers() -> None:
    """Import modules that register handlers (needed by standalone workers)."""
    from .billing import service  # noqa: F401
    from .qc import engine  # noqa: F401


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_seconds(attempt: int) -> float:
    """Capped exponential backoff with 'equal jitter' (50–100% of the step)."""
    step = min(settings.job_backoff_cap_seconds,
               settings.job_backoff_base_seconds * (2 ** max(attempt - 1, 0)))
    return step * (0.5 + random.random() / 2)


# ------------------------------------------------------------------ enqueue

def enqueue(db: Session, kind: str, payload: dict, *, dedupe_key: str | None = None,
            run_at: datetime | None = None, max_attempts: int | None = None) -> tuple[Job, bool]:
    """Add a job to the caller's transaction. Returns (job, created).

    With a dedupe_key, an existing job is returned instead of a duplicate. A
    concurrent duplicate insert rolls the session back, so call deduped
    enqueues at the start of a transaction (as the webhook inbox does).
    """
    if dedupe_key:
        existing = db.execute(select(Job).where(Job.dedupe_key == dedupe_key)).scalar_one_or_none()
        if existing is not None:
            return existing, False
    job = Job(kind=kind, payload=payload, dedupe_key=dedupe_key, run_at=run_at or _now(),
              max_attempts=max_attempts or settings.job_max_attempts)
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        if not dedupe_key:
            raise
        db.rollback()
        existing = db.execute(select(Job).where(Job.dedupe_key == dedupe_key)).scalar_one()
        return existing, False
    return job, True


# ------------------------------------------------------------------ claim / run

def _claimable(now: datetime):
    return or_(
        and_(Job.status == JobStatus.QUEUED.value, Job.run_at <= now),
        and_(Job.status == JobStatus.RUNNING.value, Job.locked_until < now),  # lease expired
    )


def claim(db: Session, *, job_id: str | None = None, limit: int = 1,
          worker_id: str = WORKER_ID) -> list[Job]:
    """Atomically take up to `limit` runnable jobs under a lease, and commit."""
    now = _now()
    q = select(Job.id).where(_claimable(now))
    if job_id:
        q = q.where(Job.id == job_id)
    q = q.order_by(Job.run_at).limit(limit).with_for_update(skip_locked=True)
    candidate_ids = list(db.execute(q).scalars())

    claimed: list[str] = []
    for jid in candidate_ids:
        res = db.execute(
            update(Job)
            .where(Job.id == jid, _claimable(now))
            .values(status=JobStatus.RUNNING.value, attempts=Job.attempts + 1,
                    locked_by=worker_id,
                    locked_until=now + timedelta(seconds=settings.job_lease_seconds),
                    updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            claimed.append(jid)
    db.commit()
    jobs = [db.get(Job, jid) for jid in claimed]
    for j in jobs:
        db.refresh(j)
    return jobs


def _settle(db: Session, job_id: str, token: int, **values) -> bool:
    """Write a terminal/retry outcome only if this attempt still owns the job."""
    res = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.attempts == token, Job.status == JobStatus.RUNNING.value)
        .values(locked_by=None, locked_until=None, updated_at=_now(), **values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return res.rowcount == 1


def execute(db: Session, job: Job) -> Job:
    """Run one claimed job and record its outcome. Never raises for handler errors."""
    token, job_id = job.attempts, job.id
    fn = _HANDLERS.get(job.kind)
    try:
        if fn is None:
            raise PermanentError(f"no handler registered for job kind '{job.kind}'")
        result = fn(db, dict(job.payload or {}))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — every failure becomes job state
        db.rollback()
        error = f"{type(exc).__name__}: {exc}"[:2000]
        permanent = isinstance(exc, PermanentError) or token >= job.max_attempts
        if permanent:
            if _settle(db, job_id, token, status=JobStatus.DEAD.value, last_error=error,
                       finished_at=_now()):
                audit.record(db, actor="job-runner", action="job.dead_lettered",
                             entity_type="job", entity_id=job_id,
                             payload={"kind": job.kind, "attempts": token, "error": error})
                db.commit()
        else:
            _settle(db, job_id, token, status=JobStatus.QUEUED.value, last_error=error,
                    run_at=_now() + timedelta(seconds=backoff_seconds(token)))
    else:
        _settle(db, job_id, token, status=JobStatus.SUCCEEDED.value, result=result,
                last_error=None, finished_at=_now())
    db.expire(job)
    return db.get(Job, job_id)


def run_inline(db: Session, job_id: str) -> Job | None:
    """Run a just-enqueued job in the current request (HDF_QUEUE_MODE=inline)."""
    jobs = claim(db, job_id=job_id)
    return execute(db, jobs[0]) if jobs else None


def maybe_run_inline(db: Session, job: Job) -> Job:
    """Run the job now if the queue is in inline mode; otherwise leave it queued."""
    if settings.queue_mode == "inline":
        return run_inline(db, job.id) or job
    return job


def work(session_factory: Callable[[], Session], *, max_jobs: int = 100,
         time_budget_s: float = 50.0, worker_id: str = WORKER_ID) -> dict:
    """Drain runnable jobs, one session per job, within a count and time budget."""
    load_handlers()
    deadline = time.monotonic() + time_budget_s
    stats = {"processed": 0, "succeeded": 0, "retrying": 0, "dead": 0}
    while stats["processed"] < max_jobs and time.monotonic() < deadline:
        with session_factory() as db:
            jobs = claim(db, worker_id=worker_id)
            if not jobs:
                break
            done = execute(db, jobs[0])
            stats["processed"] += 1
            key = {JobStatus.SUCCEEDED.value: "succeeded", JobStatus.DEAD.value: "dead"}.get(
                done.status, "retrying")
            stats[key] += 1
    return stats


# ------------------------------------------------------------------ operations

def requeue(db: Session, job: Job, *, actor: str) -> Job:
    if job.status != JobStatus.DEAD.value:
        raise ValueError("only dead-lettered jobs can be requeued")
    job.status = JobStatus.QUEUED.value
    job.attempts = 0
    job.run_at = _now()
    job.finished_at = None
    audit.record(db, actor=actor, action="job.requeued", entity_type="job", entity_id=job.id,
                 payload={"kind": job.kind, "last_error": job.last_error})
    db.commit()
    return job


def queue_stats(db: Session) -> dict:
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    oldest = db.execute(
        select(func.min(Job.run_at)).where(Job.status == JobStatus.QUEUED.value)
    ).scalar()
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return {
        "by_status": {s.value: counts.get(s.value, 0) for s in JobStatus},
        "oldest_queued_age_s": (
            round((_now() - oldest).total_seconds(), 1) if oldest else None
        ),
    }


def job_out(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "status": job.status, "attempts": job.attempts,
            "max_attempts": job.max_attempts, "run_at": job.run_at,
            "last_error": job.last_error, "result": job.result,
            "dedupe_key": job.dedupe_key, "created_at": job.created_at,
            "finished_at": job.finished_at}
