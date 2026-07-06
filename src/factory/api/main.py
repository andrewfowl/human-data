"""HTTP API for the human data factory.

Attribution: mutating endpoints require an `X-Actor-Id` header identifying who
is acting; it feeds the audit trail. Replace with real authentication (OIDC /
API keys) before production use — the control layer only needs a stable,
non-spoofable actor identity.
"""

from __future__ import annotations

from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, controls, db as database, export
from ..config import settings
from ..models import (
    DomainTrack, Expert, ExpertStatus, ExportBatch, Project, Qualification,
    Review, Submission, SubmissionStatus, Task, TaskStatus, TaskType,
)
from ..qc import engine
from ..rubrics import RUBRICS, default_rubric_for

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Human Data Factory — Finance & Accounting",
    description="Boutique expert-data pipeline with embedded internal controls "
                "and autonomous quality-control reviews.",
    version="0.1.0",
    lifespan=_lifespan,
)


def get_db() -> Iterator[Session]:
    s = database.session()
    try:
        yield s
    finally:
        s.close()


def actor_id(x_actor_id: str = Header(..., description="Acting user id for audit attribution")) -> str:
    return x_actor_id


def _get_or_404(db: Session, model, id_: str):
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


# ------------------------------------------------------------------ experts

class ExpertIn(BaseModel):
    name: str
    email: EmailStr
    credentials: list[str] = Field(default_factory=list)
    is_reviewer: bool = False


class ExamIn(BaseModel):
    track: DomainTrack
    exam_score: float = Field(ge=0, le=100)


EXAM_PASS_SCORE = 85.0


@app.post("/experts", status_code=201)
def create_expert(body: ExpertIn, actor: str = Depends(actor_id), db: Session = Depends(get_db)):
    if db.execute(select(Expert).where(Expert.email == body.email)).scalar_one_or_none():
        raise HTTPException(409, "email already registered")
    ex = Expert(name=body.name, email=body.email, credentials=body.credentials,
                is_reviewer=body.is_reviewer)
    db.add(ex)
    db.flush()
    audit.record(db, actor=actor, action="expert.created", entity_type="expert",
                 entity_id=ex.id, payload={"email": body.email})
    db.commit()
    return _expert_out(ex)


@app.post("/experts/{expert_id}/exams", status_code=201)
def record_exam(expert_id: str, body: ExamIn, actor: str = Depends(actor_id),
                db: Session = Depends(get_db)):
    ex = _get_or_404(db, Expert, expert_id)
    passed = body.exam_score >= EXAM_PASS_SCORE
    q = Qualification(expert_id=ex.id, track=body.track.value,
                      exam_score=body.exam_score, passed=passed)
    db.add(q)
    if passed and ex.status == ExpertStatus.PENDING.value:
        ex.status = ExpertStatus.QUALIFIED.value
    audit.record(db, actor=actor, action="expert.exam_recorded", entity_type="expert",
                 entity_id=ex.id, payload={"track": body.track.value,
                                           "score": body.exam_score, "passed": passed})
    db.commit()
    return {"qualification_id": q.id, "passed": passed, "expert_status": ex.status}


@app.get("/experts/{expert_id}")
def get_expert(expert_id: str, db: Session = Depends(get_db)):
    return _expert_out(_get_or_404(db, Expert, expert_id))


def _expert_out(ex: Expert) -> dict:
    return {"id": ex.id, "name": ex.name, "email": ex.email, "credentials": ex.credentials,
            "status": ex.status, "is_reviewer": ex.is_reviewer,
            "quality_score": ex.quality_score, "approved_count": ex.approved_count,
            "gold_pass_count": ex.gold_pass_count, "gold_fail_count": ex.gold_fail_count,
            "human_review_sampling_rate": controls.sampling_rate(ex)}


# ------------------------------------------------------------------ projects & tasks

class ProjectIn(BaseModel):
    name: str
    client: str
    track: DomainTrack
    task_type: TaskType
    guidelines: str = ""
    rubric_id: str | None = None


class TaskIn(BaseModel):
    prompt: str
    context: dict = Field(default_factory=dict)
    is_gold: bool = False
    gold_answer: dict | None = None


@app.post("/projects", status_code=201)
def create_project(body: ProjectIn, actor: str = Depends(actor_id), db: Session = Depends(get_db)):
    rubric_id = body.rubric_id or default_rubric_for(body.task_type.value)
    if rubric_id not in RUBRICS:
        raise HTTPException(422, f"unknown rubric '{rubric_id}'")
    p = Project(name=body.name, client=body.client, track=body.track.value,
                task_type=body.task_type.value, guidelines=body.guidelines,
                rubric_id=rubric_id, created_by=actor)
    db.add(p)
    db.flush()
    audit.record(db, actor=actor, action="project.created", entity_type="project",
                 entity_id=p.id, payload={"name": body.name, "client": body.client})
    db.commit()
    return {"id": p.id, "rubric_id": rubric_id}


@app.post("/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: str, body: TaskIn, actor: str = Depends(actor_id),
                db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    if body.is_gold and not body.gold_answer:
        raise HTTPException(422, "gold tasks require a gold_answer")
    t = Task(project_id=p.id, prompt=body.prompt, context=body.context,
             is_gold=body.is_gold, gold_answer=body.gold_answer)
    db.add(t)
    db.flush()
    audit.record(db, actor=actor, action="task.created", entity_type="task",
                 entity_id=t.id, payload={"project_id": p.id, "is_gold": body.is_gold})
    db.commit()
    return {"id": t.id}


@app.post("/tasks/{task_id}/assign/{expert_id}")
def assign_task(task_id: str, expert_id: str, actor: str = Depends(actor_id),
                db: Session = Depends(get_db)):
    t = _get_or_404(db, Task, task_id)
    ex = _get_or_404(db, Expert, expert_id)
    p = db.get(Project, t.project_id)
    try:
        controls.assert_qualified(db, ex, p.track)  # C1
    except controls.ControlViolation as e:
        raise HTTPException(403, str(e))
    t.assigned_to = ex.id
    t.status = TaskStatus.ASSIGNED.value
    audit.record(db, actor=actor, action="task.assigned", entity_type="task",
                 entity_id=t.id, payload={"expert_id": ex.id})
    db.commit()
    return {"task_id": t.id, "assigned_to": ex.id}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    t = _get_or_404(db, Task, task_id)
    # gold_answer is deliberately never returned — honeypots stay blind.
    return {"id": t.id, "project_id": t.project_id, "prompt": t.prompt,
            "context": t.context, "status": t.status, "assigned_to": t.assigned_to,
            "is_gold": t.is_gold}


# ------------------------------------------------------------------ submissions & reviews

class SubmissionIn(BaseModel):
    expert_id: str
    content: dict


class HumanReviewIn(BaseModel):
    reviewer_id: str
    verdict: Literal["pass", "revise", "fail"]
    criterion_scores: list[dict] | None = None
    comments: str = ""


@app.post("/tasks/{task_id}/submissions", status_code=201)
def create_submission(task_id: str, body: SubmissionIn, db: Session = Depends(get_db)):
    t = _get_or_404(db, Task, task_id)
    ex = _get_or_404(db, Expert, body.expert_id)
    try:
        sub = engine.submit(db, task=t, expert=ex, content=body.content)
    except (controls.ControlViolation, engine.PipelineError) as e:
        raise HTTPException(403, str(e))
    return _submission_out(db, sub)


@app.get("/submissions/{submission_id}")
def get_submission(submission_id: str, db: Session = Depends(get_db)):
    return _submission_out(db, _get_or_404(db, Submission, submission_id))


@app.post("/submissions/{submission_id}/human-review", status_code=201)
def post_human_review(submission_id: str, body: HumanReviewIn, db: Session = Depends(get_db)):
    sub = _get_or_404(db, Submission, submission_id)
    reviewer = _get_or_404(db, Expert, body.reviewer_id)
    try:
        rev = engine.human_review(db, submission=sub, reviewer=reviewer,
                                  verdict=body.verdict,
                                  criterion_scores=body.criterion_scores,
                                  comments=body.comments)
    except (controls.ControlViolation, engine.PipelineError) as e:
        raise HTTPException(403, str(e))
    return {"review_id": rev.id, "verdict": rev.verdict,
            "submission_status": sub.status}


@app.get("/review-queue")
def review_queue(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Submission).where(Submission.status == SubmissionStatus.HUMAN_REVIEW.value)
        .order_by(Submission.created_at)
    ).scalars().all()
    # Author identity withheld: reviews are blind (supports C2's spirit).
    return [{"submission_id": s.id, "task_id": s.task_id,
             "sampled": s.sampled_for_human_review, "created_at": s.created_at}
            for s in rows]


def _submission_out(db: Session, sub: Submission) -> dict:
    reviews = db.execute(
        select(Review).where(Review.submission_id == sub.id).order_by(Review.created_at)
    ).scalars().all()
    return {
        "id": sub.id, "task_id": sub.task_id, "expert_id": sub.expert_id,
        "version": sub.version, "status": sub.status,
        "sampled_for_human_review": sub.sampled_for_human_review,
        "reviews": [{"id": r.id, "kind": r.kind, "reviewer_id": r.reviewer_id,
                     "verdict": r.verdict, "overall_score": r.overall_score,
                     "detail": r.detail} for r in reviews],
    }


# ------------------------------------------------------------------ exports & oversight

class ExportApproveIn(BaseModel):
    approved_by: str


@app.get("/projects/{project_id}/control-report")
def get_control_report(project_id: str, db: Session = Depends(get_db)):
    return export.control_report(db, _get_or_404(db, Project, project_id))


@app.post("/projects/{project_id}/exports", status_code=201)
def request_export(project_id: str, actor: str = Depends(actor_id), db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    try:
        batch = export.request_export(db, project=p, requested_by=actor)
    except export.ExportError as e:
        raise HTTPException(409, str(e))
    return {"batch_id": batch.id, "status": batch.status}


@app.post("/exports/{batch_id}/approve")
def approve_export(batch_id: str, body: ExportApproveIn, db: Session = Depends(get_db)):
    batch = _get_or_404(db, ExportBatch, batch_id)
    try:
        batch = export.approve_and_materialize(db, batch=batch, approved_by=body.approved_by)
    except (export.ExportError, controls.ControlViolation) as e:
        raise HTTPException(409, str(e))
    return {"batch_id": batch.id, "status": batch.status, "path": batch.path,
            "manifest": batch.manifest}


@app.get("/audit/verify")
def verify_audit(db: Session = Depends(get_db)):
    ok, n = audit.verify_chain(db)
    return {"intact": ok, "events": n}


@app.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    by_status = dict(db.execute(
        select(Submission.status, func.count()).group_by(Submission.status)
    ).all())
    total = sum(by_status.values())
    experts_by_status = dict(db.execute(
        select(Expert.status, func.count()).group_by(Expert.status)
    ).all())
    return {
        "total_submissions": total,
        "submissions_by_status": by_status,
        "approval_rate": round(by_status.get("approved", 0) / total, 3) if total else None,
        "human_review_backlog": by_status.get("human_review", 0),
        "experts_by_status": experts_by_status,
        "qc_model": settings.qc_model,
    }
