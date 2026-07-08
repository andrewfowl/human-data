"""HTTP API for the human data factory.

Authentication: `Authorization: Bearer hdf_...` API keys (see factory/auth.py).
Cold start: `POST /bootstrap` creates the first admin user + key while the
users table is empty. `HDF_AUTH_DISABLED=1` switches to header-based dev mode.

Roles: admin (everything), ops (production + billing ops), reviewer (review
queue), expert (submissions), client (read-only, scoped to their own firm).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, auth, controls, db as database, export
from ..billing import service as billing
from ..billing.stripe_gateway import get_gateway
from ..config import settings
from ..models import (
    ApiKey, BillingMode, DomainTrack, Expert, ExpertStatus, ExportBatch, Firm,
    Invoice, Project, Qualification, RateCard, Review, Role, Submission,
    SubmissionStatus, Task, TaskStatus, TaskType, UsageEvent, User,
)
from ..qc import engine
from ..rubrics import RUBRICS, default_rubric_for


@asynccontextmanager
async def _lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Human Data Factory — Finance & Accounting",
    description="Boutique expert-data pipeline with embedded internal controls, "
                "autonomous quality-control reviews, and firm-level billing "
                "(external or embedded Stripe).",
    version="0.2.0",
    lifespan=_lifespan,
)


def get_db() -> Iterator[Session]:
    s = database.session()
    try:
        yield s
    finally:
        s.close()


current_actor = auth.make_actor_dependency(get_db)

ADMIN = Role.ADMIN.value
OPS = Role.OPS.value
REVIEWER = Role.REVIEWER.value
EXPERT = Role.EXPERT.value
CLIENT = Role.CLIENT.value

internal = auth.require_roles(current_actor, ADMIN, OPS)
admin_only = auth.require_roles(current_actor, ADMIN)
review_roles = auth.require_roles(current_actor, ADMIN, OPS, REVIEWER)
submit_roles = auth.require_roles(current_actor, ADMIN, OPS, EXPERT)
any_authenticated = auth.require_roles(current_actor, ADMIN, OPS, REVIEWER, EXPERT, CLIENT)


def _get_or_404(db: Session, model, id_: str):
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


# ------------------------------------------------------------------ bootstrap & users

class BootstrapIn(BaseModel):
    name: str
    email: EmailStr


class UserIn(BaseModel):
    name: str
    email: EmailStr
    role: Role
    firm_id: str | None = None
    expert_id: str | None = None


@app.post("/bootstrap", status_code=201)
def bootstrap(body: BootstrapIn, db: Session = Depends(get_db)):
    """Create the first admin user + API key. Works only on an empty user table."""
    if db.execute(select(func.count()).select_from(User)).scalar():
        raise HTTPException(409, "already bootstrapped")
    user = User(name=body.name, email=body.email, role=ADMIN)
    db.add(user)
    db.flush()
    key = auth.issue_key(db, user)
    audit.record(db, actor=user.id, action="user.bootstrapped", entity_type="user",
                 entity_id=user.id, payload={"email": body.email})
    db.commit()
    return {"user_id": user.id, "api_key": key,
            "note": "store this key now; it is not retrievable later"}


@app.post("/users", status_code=201)
def create_user(body: UserIn, actor: auth.Actor = Depends(admin_only),
                db: Session = Depends(get_db)):
    if db.execute(select(User).where(User.email == body.email)).scalar_one_or_none():
        raise HTTPException(409, "email already registered")
    if body.role == Role.CLIENT and not body.firm_id:
        raise HTTPException(422, "client users must belong to a firm")
    if body.role in (Role.REVIEWER, Role.EXPERT) and not body.expert_id:
        raise HTTPException(422, f"{body.role.value} users must link an expert record")
    if body.firm_id:
        _get_or_404(db, Firm, body.firm_id)
    if body.expert_id:
        _get_or_404(db, Expert, body.expert_id)
    user = User(name=body.name, email=body.email, role=body.role.value,
                firm_id=body.firm_id, expert_id=body.expert_id)
    db.add(user)
    db.flush()
    key = auth.issue_key(db, user)
    audit.record(db, actor=actor.id, action="user.created", entity_type="user",
                 entity_id=user.id, payload={"email": body.email, "role": body.role.value})
    db.commit()
    return {"user_id": user.id, "api_key": key,
            "note": "store this key now; it is not retrievable later"}


@app.post("/users/{user_id}/revoke")
def revoke_user(user_id: str, actor: auth.Actor = Depends(admin_only),
                db: Session = Depends(get_db)):
    user = _get_or_404(db, User, user_id)
    user.active = False
    for k in db.execute(select(ApiKey).where(ApiKey.user_id == user.id)).scalars():
        k.active = False
    audit.record(db, actor=actor.id, action="user.revoked", entity_type="user",
                 entity_id=user.id)
    db.commit()
    return {"user_id": user.id, "active": False}


# ------------------------------------------------------------------ firms

class FirmIn(BaseModel):
    name: str
    billing_email: EmailStr
    billing_mode: BillingMode = BillingMode.EXTERNAL
    currency: str = "usd"
    external_reference: str = ""


class RateCardIn(BaseModel):
    task_type: TaskType
    unit_price_cents: int = Field(gt=0)


@app.post("/firms", status_code=201)
def create_firm(body: FirmIn, actor: auth.Actor = Depends(admin_only),
                db: Session = Depends(get_db)):
    if db.execute(select(Firm).where(Firm.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, "firm name already exists")
    firm = Firm(name=body.name, billing_email=body.billing_email,
                billing_mode=body.billing_mode.value, currency=body.currency,
                external_reference=body.external_reference)
    db.add(firm)
    db.flush()
    audit.record(db, actor=actor.id, action="firm.created", entity_type="firm",
                 entity_id=firm.id, payload={"name": body.name,
                                             "billing_mode": body.billing_mode.value})
    db.commit()
    return _firm_out(firm)


@app.get("/firms")
def list_firms(actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    return [_firm_out(f) for f in db.execute(select(Firm)).scalars()]


@app.get("/firms/{firm_id}")
def get_firm(firm_id: str, actor: auth.Actor = Depends(any_authenticated),
             db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id)
    return _firm_out(_get_or_404(db, Firm, firm_id))


@app.post("/firms/{firm_id}/rate-cards", status_code=201)
def create_rate_card(firm_id: str, body: RateCardIn,
                     actor: auth.Actor = Depends(admin_only), db: Session = Depends(get_db)):
    firm = _get_or_404(db, Firm, firm_id)
    # Newest active card wins; deactivate prior cards for the same task type.
    for old in db.execute(select(RateCard).where(
            RateCard.firm_id == firm.id, RateCard.task_type == body.task_type.value,
            RateCard.active.is_(True))).scalars():
        old.active = False
    card = RateCard(firm_id=firm.id, task_type=body.task_type.value,
                    unit_price_cents=body.unit_price_cents)
    db.add(card)
    db.flush()
    audit.record(db, actor=actor.id, action="rate_card.created", entity_type="rate_card",
                 entity_id=card.id, payload={"firm_id": firm.id,
                                             "task_type": body.task_type.value,
                                             "unit_price_cents": body.unit_price_cents})
    db.commit()
    return {"id": card.id, "task_type": card.task_type,
            "unit_price_cents": card.unit_price_cents}


def _firm_out(f: Firm) -> dict:
    return {"id": f.id, "name": f.name, "billing_email": f.billing_email,
            "billing_mode": f.billing_mode, "currency": f.currency,
            "external_reference": f.external_reference,
            "stripe_customer_id": f.stripe_customer_id}


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
def create_expert(body: ExpertIn, actor: auth.Actor = Depends(internal),
                  db: Session = Depends(get_db)):
    if db.execute(select(Expert).where(Expert.email == body.email)).scalar_one_or_none():
        raise HTTPException(409, "email already registered")
    ex = Expert(name=body.name, email=body.email, credentials=body.credentials,
                is_reviewer=body.is_reviewer)
    db.add(ex)
    db.flush()
    audit.record(db, actor=actor.id, action="expert.created", entity_type="expert",
                 entity_id=ex.id, payload={"email": body.email})
    db.commit()
    return _expert_out(ex)


@app.post("/experts/{expert_id}/exams", status_code=201)
def record_exam(expert_id: str, body: ExamIn, actor: auth.Actor = Depends(internal),
                db: Session = Depends(get_db)):
    ex = _get_or_404(db, Expert, expert_id)
    passed = body.exam_score >= EXAM_PASS_SCORE
    q = Qualification(expert_id=ex.id, track=body.track.value,
                      exam_score=body.exam_score, passed=passed)
    db.add(q)
    if passed and ex.status == ExpertStatus.PENDING.value:
        ex.status = ExpertStatus.QUALIFIED.value
    audit.record(db, actor=actor.id, action="expert.exam_recorded", entity_type="expert",
                 entity_id=ex.id, payload={"track": body.track.value,
                                           "score": body.exam_score, "passed": passed})
    db.commit()
    return {"qualification_id": q.id, "passed": passed, "expert_status": ex.status}


@app.get("/experts/{expert_id}")
def get_expert(expert_id: str, actor: auth.Actor = Depends(internal),
               db: Session = Depends(get_db)):
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
    firm_id: str
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
def create_project(body: ProjectIn, actor: auth.Actor = Depends(internal),
                   db: Session = Depends(get_db)):
    _get_or_404(db, Firm, body.firm_id)
    rubric_id = body.rubric_id or default_rubric_for(body.task_type.value)
    if rubric_id not in RUBRICS:
        raise HTTPException(422, f"unknown rubric '{rubric_id}'")
    p = Project(name=body.name, firm_id=body.firm_id, track=body.track.value,
                task_type=body.task_type.value, guidelines=body.guidelines,
                rubric_id=rubric_id, created_by=actor.id)
    db.add(p)
    db.flush()
    audit.record(db, actor=actor.id, action="project.created", entity_type="project",
                 entity_id=p.id, payload={"name": body.name, "firm_id": body.firm_id})
    db.commit()
    return {"id": p.id, "rubric_id": rubric_id}


@app.get("/projects")
def list_projects(actor: auth.Actor = Depends(any_authenticated),
                  db: Session = Depends(get_db)):
    q = select(Project)
    if actor.role == CLIENT:
        q = q.where(Project.firm_id == actor.firm_id)
    return [{"id": p.id, "name": p.name, "firm_id": p.firm_id, "track": p.track,
             "task_type": p.task_type} for p in db.execute(q).scalars()]


@app.post("/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: str, body: TaskIn, actor: auth.Actor = Depends(internal),
                db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    if body.is_gold and not body.gold_answer:
        raise HTTPException(422, "gold tasks require a gold_answer")
    t = Task(project_id=p.id, prompt=body.prompt, context=body.context,
             is_gold=body.is_gold, gold_answer=body.gold_answer)
    db.add(t)
    db.flush()
    audit.record(db, actor=actor.id, action="task.created", entity_type="task",
                 entity_id=t.id, payload={"project_id": p.id, "is_gold": body.is_gold})
    db.commit()
    return {"id": t.id}


@app.post("/tasks/{task_id}/assign/{expert_id}")
def assign_task(task_id: str, expert_id: str, actor: auth.Actor = Depends(internal),
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
    audit.record(db, actor=actor.id, action="task.assigned", entity_type="task",
                 entity_id=t.id, payload={"expert_id": ex.id})
    db.commit()
    return {"task_id": t.id, "assigned_to": ex.id}


@app.get("/tasks/{task_id}")
def get_task(task_id: str, actor: auth.Actor = Depends(any_authenticated),
             db: Session = Depends(get_db)):
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
def create_submission(task_id: str, body: SubmissionIn,
                      actor: auth.Actor = Depends(submit_roles),
                      db: Session = Depends(get_db)):
    if actor.role == EXPERT and body.expert_id != actor.expert_id:
        raise HTTPException(403, "experts may only submit as themselves")
    t = _get_or_404(db, Task, task_id)
    ex = _get_or_404(db, Expert, body.expert_id)
    try:
        sub = engine.submit(db, task=t, expert=ex, content=body.content)
    except (controls.ControlViolation, engine.PipelineError) as e:
        raise HTTPException(403, str(e))
    return _submission_out(db, sub)


@app.get("/submissions/{submission_id}")
def get_submission(submission_id: str, actor: auth.Actor = Depends(review_roles),
                   db: Session = Depends(get_db)):
    return _submission_out(db, _get_or_404(db, Submission, submission_id))


@app.post("/submissions/{submission_id}/human-review", status_code=201)
def post_human_review(submission_id: str, body: HumanReviewIn,
                      actor: auth.Actor = Depends(review_roles),
                      db: Session = Depends(get_db)):
    if actor.role == REVIEWER and body.reviewer_id != actor.expert_id:
        raise HTTPException(403, "reviewers may only review as themselves")
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
def review_queue(actor: auth.Actor = Depends(review_roles), db: Session = Depends(get_db)):
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


# ------------------------------------------------------------------ billing

class InvoiceGenerateIn(BaseModel):
    period_start: datetime
    period_end: datetime


class ExternalPaymentIn(BaseModel):
    reference: str


@app.get("/firms/{firm_id}/billing")
def get_billing_summary(firm_id: str, actor: auth.Actor = Depends(any_authenticated),
                        db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id)
    return billing.firm_billing_summary(db, _get_or_404(db, Firm, firm_id))


@app.get("/firms/{firm_id}/usage")
def list_usage(firm_id: str, uninvoiced_only: bool = False,
               actor: auth.Actor = Depends(any_authenticated),
               db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id)
    _get_or_404(db, Firm, firm_id)
    q = select(UsageEvent).where(UsageEvent.firm_id == firm_id)
    if uninvoiced_only:
        q = q.where(UsageEvent.invoice_id.is_(None))
    return [{"id": e.id, "project_id": e.project_id, "task_id": e.task_id,
             "kind": e.kind, "quantity": e.quantity,
             "unit_price_cents": e.unit_price_cents, "amount_cents": e.amount_cents,
             "currency": e.currency, "invoice_id": e.invoice_id,
             "created_at": e.created_at}
            for e in db.execute(q.order_by(UsageEvent.created_at)).scalars()]


@app.post("/firms/{firm_id}/invoices", status_code=201)
def generate_invoice(firm_id: str, body: InvoiceGenerateIn,
                     actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    firm = _get_or_404(db, Firm, firm_id)
    try:
        inv = billing.generate_invoice(db, firm=firm, period_start=body.period_start,
                                       period_end=body.period_end, issued_by=actor.id)
    except billing.BillingError as e:
        raise HTTPException(409, str(e))
    return _invoice_out(inv)


@app.get("/firms/{firm_id}/invoices")
def list_invoices(firm_id: str, actor: auth.Actor = Depends(any_authenticated),
                  db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id)
    _get_or_404(db, Firm, firm_id)
    rows = db.execute(select(Invoice).where(Invoice.firm_id == firm_id)
                      .order_by(Invoice.created_at)).scalars()
    return [_invoice_out(i) for i in rows]


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, actor: auth.Actor = Depends(any_authenticated),
                db: Session = Depends(get_db)):
    inv = _get_or_404(db, Invoice, invoice_id)
    auth.assert_firm_access(actor, inv.firm_id)
    return _invoice_out(inv)


@app.post("/invoices/{invoice_id}/external-payment")
def record_external_payment(invoice_id: str, body: ExternalPaymentIn,
                            actor: auth.Actor = Depends(internal),
                            db: Session = Depends(get_db)):
    inv = _get_or_404(db, Invoice, invoice_id)
    try:
        inv = billing.record_external_payment(db, invoice=inv, reference=body.reference,
                                              actor=actor.id)
    except billing.BillingError as e:
        raise HTTPException(409, str(e))
    return _invoice_out(inv)


@app.get("/firms/{firm_id}/reconciliation")
def firm_reconciliation(firm_id: str, period_start: datetime | None = None,
                        period_end: datetime | None = None,
                        actor: auth.Actor = Depends(any_authenticated),
                        db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id)
    firm = _get_or_404(db, Firm, firm_id)
    return billing.reconciliation_report(db, firm, period_start, period_end)


@app.get("/billing/reconciliation")
def global_reconciliation(period_start: datetime | None = None,
                          period_end: datetime | None = None,
                          actor: auth.Actor = Depends(internal),
                          db: Session = Depends(get_db)):
    firms = db.execute(select(Firm)).scalars().all()
    reports = [billing.reconciliation_report(db, f, period_start, period_end)
               for f in firms]
    return {
        "firms": reports,
        "totals": {
            k: sum(r[k] for r in reports)
            for k in ("metered_cents", "metered_uninvoiced_cents", "invoiced_cents",
                      "paid_cents", "outstanding_cents")
        },
        "all_clean": all(r["clean"] for r in reports),
    }


@app.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db),
                         stripe_signature: str | None = Header(None)):
    payload = await request.body()
    gateway = get_gateway()
    try:
        event = gateway.parse_webhook(payload, stripe_signature)
    except PermissionError as e:
        raise HTTPException(503, str(e))
    except Exception:
        raise HTTPException(400, "invalid webhook payload or signature")
    return billing.handle_stripe_event(db, event)


def _invoice_out(inv: Invoice) -> dict:
    return {"id": inv.id, "number": inv.number, "firm_id": inv.firm_id,
            "mode": inv.mode, "status": inv.status,
            "period_start": inv.period_start, "period_end": inv.period_end,
            "subtotal_cents": inv.subtotal_cents, "currency": inv.currency,
            "line_items": inv.line_items,
            "stripe_invoice_id": inv.stripe_invoice_id,
            "hosted_invoice_url": inv.hosted_invoice_url,
            "external_paid_reference": inv.external_paid_reference,
            "issued_at": inv.issued_at, "paid_at": inv.paid_at}


# ------------------------------------------------------------------ exports & oversight

class ExportApproveIn(BaseModel):
    approved_by: str


@app.get("/projects/{project_id}/control-report")
def get_control_report(project_id: str, actor: auth.Actor = Depends(any_authenticated),
                       db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    auth.assert_firm_access(actor, p.firm_id)
    return export.control_report(db, p)


@app.post("/projects/{project_id}/exports", status_code=201)
def request_export(project_id: str, actor: auth.Actor = Depends(internal),
                   db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    try:
        batch = export.request_export(db, project=p, requested_by=actor.id)
    except export.ExportError as e:
        raise HTTPException(409, str(e))
    return {"batch_id": batch.id, "status": batch.status}


@app.post("/exports/{batch_id}/approve")
def approve_export(batch_id: str, body: ExportApproveIn,
                   actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    batch = _get_or_404(db, ExportBatch, batch_id)
    # In authenticated mode the approver is the caller; the body field remains
    # for dev mode where identity is header-supplied.
    approver = actor.id if not settings.auth_disabled else body.approved_by
    try:
        batch = export.approve_and_materialize(db, batch=batch, approved_by=approver)
    except (export.ExportError, controls.ControlViolation) as e:
        raise HTTPException(409, str(e))
    return {"batch_id": batch.id, "status": batch.status, "path": batch.path,
            "manifest": batch.manifest}


@app.get("/audit/verify")
def verify_audit(actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    ok, n = audit.verify_chain(db)
    return {"intact": ok, "events": n}


@app.get("/metrics")
def metrics(actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    by_status = dict(db.execute(
        select(Submission.status, func.count()).group_by(Submission.status)
    ).all())
    total = sum(by_status.values())
    experts_by_status = dict(db.execute(
        select(Expert.status, func.count()).group_by(Expert.status)
    ).all())
    uninvoiced = db.execute(
        select(func.coalesce(func.sum(UsageEvent.amount_cents), 0))
        .where(UsageEvent.invoice_id.is_(None))
    ).scalar()
    return {
        "total_submissions": total,
        "submissions_by_status": by_status,
        "approval_rate": round(by_status.get("approved", 0) / total, 3) if total else None,
        "human_review_backlog": by_status.get("human_review", 0),
        "experts_by_status": experts_by_status,
        "uninvoiced_usage_cents": int(uninvoiced or 0),
        "qc_model": settings.qc_model,
    }


# ------------------------------------------------------------------ landing page

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Human Data Factory</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: Georgia, 'Times New Roman', serif; max-width: 44rem;
         margin: 4rem auto; padding: 0 1.25rem; line-height: 1.6;
         background: #faf7f2; color: #1f1b16; }
  @media (prefers-color-scheme: dark) { body { background: #16130f; color: #ece7df; } }
  h1 { font-size: 1.9rem; margin-bottom: .25rem; }
  .tag { font-style: italic; opacity: .75; margin-top: 0; }
  ul { padding-left: 1.2rem; }
  a { color: #a05a2c; }
  code { font-family: ui-monospace, monospace; font-size: .9em;
         background: rgba(160,90,44,.12); padding: .1em .35em; border-radius: 4px; }
</style></head><body>
<h1>Human Data Factory</h1>
<p class="tag">Finance &amp; accounting training datasets, produced under audit-grade internal controls.</p>
<ul>
  <li>Vetted CPA/CFA experts, qualification-gated by domain track</li>
  <li>Five-gate QC: deterministic validators &rarr; gold calibration &rarr; autonomous
      Claude review &rarr; sampled human review &rarr; dual-control release</li>
  <li>Firm-level billing: metered usage with external invoicing, or embedded Stripe</li>
  <li>Hash-chained audit trail behind every record shipped</li>
</ul>
<p><a href="/docs">API documentation</a> &middot; <a href="/healthz">health</a></p>
<p>New deployment? <code>POST /bootstrap</code> creates the first admin user and API key.</p>
</body></html>"""


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(select(func.count()).select_from(User))
    return {"ok": True, "version": app.version}
