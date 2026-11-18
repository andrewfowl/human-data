"""HTTP API for the human data factory.

Authentication: `Authorization: Bearer hdf_...` API keys (see factory/auth.py).
Cold start: `POST /bootstrap` creates the first admin user + key while the
users table is empty. `HDF_AUTH_DISABLED=1` switches to header-based dev mode.

Roles: admin (everything), ops (production + billing ops), reviewer (review
queue), expert (submissions), client (read-only, scoped to their own firm).

Tenancy: each firm is a tenant. A client user belongs to one or more firms via
FirmMembership (owner > admin > member) and picks the active firm with the
`X-Firm-Id` header (default: their home firm). Owners/admins see every firm
project and billing; members see only projects granted via ProjectMembership.
On Postgres, client requests additionally run under row-level security
(factory/tenancy.py), so cross-firm rows are invisible at the database level.
Experts are a shared pool across firms.
"""

from __future__ import annotations

import hmac

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, auth, controls, db as database, export, jobs
from ..billing import service as billing
from ..billing.stripe_gateway import get_gateway
from ..config import settings
from ..models import (
    FIRM_ROLE_RANK, ApiKey, BillingMode, DomainTrack, Expert, ExpertStatus, ExportBatch, Firm,
    FirmMembership, FirmRole, Invoice, Job, JobStatus, Project, ProjectMembership,
    Qualification, RateCard, Review, Role, Submission, SubmissionStatus, Task, TaskStatus,
    TaskType, UsageEvent, User,
)
from ..qc import calibration, engine
from ..idempotency import IdempotencyMiddleware, purge_expired as purge_idempotency_keys
from ..ratelimit import RateLimitMiddleware
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
    version="0.4.0",
    lifespan=_lifespan,
    # Deep links write "#/tag/operation" hashes, which the v0 preview runtime passes to
    # querySelector and throws on. Expanding operations still works without them.
    swagger_ui_parameters={"deepLinking": False},
)
# Starlette wraps in reverse order: the rate limiter runs first, so replays count too.
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimitMiddleware)


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
    firm_role: FirmRole | None = None  # client only; default owner for a firm's first user


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
    firm_role = None
    if body.role == Role.CLIENT:
        firm_role = (body.firm_role or (FirmRole.MEMBER if _firm_has_members(db, body.firm_id)
                                        else FirmRole.OWNER)).value
        db.add(FirmMembership(firm_id=body.firm_id, user_id=user.id, firm_role=firm_role))
    key = auth.issue_key(db, user)
    audit.record(db, actor=actor.id, action="user.created", entity_type="user",
                 entity_id=user.id, payload={"email": body.email, "role": body.role.value,
                                             "firm_role": firm_role})
    db.commit()
    return {"user_id": user.id, "api_key": key, "firm_role": firm_role,
            "note": "store this key now; it is not retrievable later"}


def _firm_has_members(db: Session, firm_id: str) -> bool:
    return db.execute(select(FirmMembership.id)
                      .where(FirmMembership.firm_id == firm_id)).first() is not None


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
    q = select(Project).order_by(Project.created_at)
    visible = auth.accessible_project_filter(actor)
    if visible is not None:
        q = q.where(visible)
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
    if actor.role == CLIENT:
        auth.assert_project_access(db, actor, db.get(Project, t.project_id))
    elif actor.role == EXPERT and t.assigned_to != actor.expert_id:
        raise HTTPException(403, "experts may only view tasks assigned to them")
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
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    return billing.firm_billing_summary(db, _get_or_404(db, Firm, firm_id))


@app.get("/firms/{firm_id}/usage")
def list_usage(firm_id: str, uninvoiced_only: bool = False,
               actor: auth.Actor = Depends(any_authenticated),
               db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
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
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    _get_or_404(db, Firm, firm_id)
    rows = db.execute(select(Invoice).where(Invoice.firm_id == firm_id)
                      .order_by(Invoice.created_at)).scalars()
    return [_invoice_out(i) for i in rows]


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, actor: auth.Actor = Depends(any_authenticated),
                db: Session = Depends(get_db)):
    inv = _get_or_404(db, Invoice, invoice_id)
    auth.assert_firm_access(actor, inv.firm_id, FirmRole.ADMIN)
    return _invoice_out(inv)


class CheckoutIn(BaseModel):
    success_url: str
    cancel_url: str


@app.post("/invoices/{invoice_id}/checkout", status_code=201)
def create_invoice_checkout(invoice_id: str, body: CheckoutIn,
                            actor: auth.Actor = Depends(any_authenticated),
                            db: Session = Depends(get_db)):
    """Self-serve payment: returns a Stripe Checkout URL for an unpaid invoice."""
    inv = _get_or_404(db, Invoice, invoice_id)
    auth.assert_firm_access(actor, inv.firm_id, FirmRole.ADMIN)
    firm = _get_or_404(db, Firm, inv.firm_id)
    try:
        inv, checkout_url = billing.create_checkout(db, invoice=inv, firm=firm,
                                                    success_url=body.success_url,
                                                    cancel_url=body.cancel_url,
                                                    actor=actor.id)
    except billing.BillingError as e:
        raise HTTPException(409, str(e))
    return {"invoice_id": inv.id, "checkout_session_id": inv.checkout_session_id,
            "checkout_url": checkout_url}


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
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
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
    # Inbox: persist the verified event, deduped on Stripe's event id, before
    # acting on it. Stripe retries anything that isn't 2xx, so once the event
    # is durably stored we acknowledge; failures are retried by our queue.
    event_id = event.get("id")
    job, created = jobs.enqueue(db, billing.STRIPE_EVENT_JOB, {"event": event},
                                dedupe_key=f"stripe:{event_id}" if event_id else None)
    db.commit()
    if created:
        job = jobs.maybe_run_inline(db, job)
    if job.status == JobStatus.SUCCEEDED.value:
        return {**(job.result or {}), "job_id": job.id, "duplicate": not created}
    return {"accepted": True, "job_id": job.id, "status": job.status, "duplicate": not created}


# ------------------------------------------------------------------ background jobs

@app.get("/jobs")
def list_jobs(status: str | None = None, kind: str | None = None, limit: int = 50,
              actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    q = select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        q = q.where(Job.status == status)
    if kind:
        q = q.where(Job.kind == kind)
    return {"stats": jobs.queue_stats(db),
            "jobs": [jobs.job_out(j) for j in db.execute(q).scalars()]}


@app.post("/jobs/{job_id}/requeue")
def requeue_job(job_id: str, actor: auth.Actor = Depends(admin_only),
                db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        job = jobs.requeue(db, job, actor=actor.id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return jobs.job_out(job)


@app.post("/jobs/run")
def run_jobs(max_jobs: int = 25, actor: auth.Actor = Depends(admin_only)):
    return jobs.work(database.session, max_jobs=max(1, min(max_jobs, 200)), time_budget_s=25)


@app.get("/cron/jobs", include_in_schema=False)
def cron_jobs(authorization: str | None = Header(None)):
    """Vercel Cron sweep: retries due jobs, reclaims expired leases, purges old keys."""
    if not settings.cron_secret:
        raise HTTPException(503, "CRON_SECRET is not configured")
    if not hmac.compare_digest(authorization or "", f"Bearer {settings.cron_secret}"):
        raise HTTPException(401, "invalid cron credentials")
    result = jobs.work(database.session, max_jobs=100, time_budget_s=45)
    return {**result, "idempotency_keys_purged": purge_idempotency_keys(database.session)}


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
    auth.assert_project_access(db, actor, p)
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


@app.get("/qc/calibration")
def qc_calibration(track: DomainTrack | None = None,
                   actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    """Auto-reviewer vs human-reviewer agreement, sliced by track and grader version."""
    return calibration.calibration_report(db, track.value if track else None)


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


# ------------------------------------------------------------------ dashboard support

@app.get("/bootstrap/status")
def bootstrap_status(db: Session = Depends(get_db)):
    count = db.execute(select(func.count()).select_from(User)).scalar()
    return {"bootstrapped": bool(count)}


@app.get("/me")
def whoami(actor: auth.Actor = Depends(any_authenticated)):
    return {"id": actor.id, "name": actor.name, "role": actor.role,
            "firm_id": actor.firm_id, "expert_id": actor.expert_id,
            "firm_role": actor.firm_role, "firm_ids": list(actor.firm_ids)}


# ------------------------------------------------------------------ firm & project members

class MemberIn(BaseModel):
    name: str
    email: EmailStr
    firm_role: FirmRole = FirmRole.MEMBER


class MemberRoleIn(BaseModel):
    firm_role: FirmRole


class ProjectMemberIn(BaseModel):
    user_id: str


def _assert_can_grant(actor: auth.Actor, firm_role: str) -> None:
    """Admins manage members; only owners (or staff) can grant/revoke admin or owner."""
    if actor.is_internal:
        return
    if FIRM_ROLE_RANK[firm_role] >= FIRM_ROLE_RANK[FirmRole.ADMIN.value] \
            and actor.firm_role != FirmRole.OWNER.value:
        raise HTTPException(403, "only firm owners can manage admins and owners")


def _membership_or_404(db: Session, firm_id: str, user_id: str) -> FirmMembership:
    m = db.execute(select(FirmMembership).where(FirmMembership.firm_id == firm_id,
                                                FirmMembership.user_id == user_id)
                   ).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "user is not a member of this firm")
    return m


def _assert_not_last_owner(db: Session, m: FirmMembership) -> None:
    if m.firm_role != FirmRole.OWNER.value:
        return
    owners = db.execute(select(func.count()).select_from(FirmMembership).where(
        FirmMembership.firm_id == m.firm_id,
        FirmMembership.firm_role == FirmRole.OWNER.value)).scalar()
    if owners <= 1:
        raise HTTPException(409, "a firm must keep at least one owner")


@app.get("/firms/{firm_id}/members")
def list_members(firm_id: str, actor: auth.Actor = Depends(any_authenticated),
                 db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    rows = db.execute(select(FirmMembership, User).join(User, FirmMembership.user_id == User.id)
                      .where(FirmMembership.firm_id == firm_id)
                      .order_by(FirmMembership.created_at)).all()
    return [{"user_id": u.id, "name": u.name, "email": u.email, "firm_role": m.firm_role,
             "active": u.active} for m, u in rows]


@app.post("/firms/{firm_id}/members", status_code=201)
def add_member(firm_id: str, body: MemberIn, actor: auth.Actor = Depends(any_authenticated),
               db: Session = Depends(get_db)):
    """Invite a colleague: creates a client user in this firm and returns their API key."""
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    _assert_can_grant(actor, body.firm_role.value)
    _get_or_404(db, Firm, firm_id)
    if db.execute(select(User.id).where(User.email == body.email)).first():
        raise HTTPException(409, "email already registered")
    user = User(name=body.name, email=body.email, role=CLIENT, firm_id=firm_id)
    db.add(user)
    db.flush()
    db.add(FirmMembership(firm_id=firm_id, user_id=user.id, firm_role=body.firm_role.value))
    key = auth.issue_key(db, user)
    audit.record(db, actor=actor.id, action="firm.member_added", entity_type="firm",
                 entity_id=firm_id, payload={"user_id": user.id,
                                             "firm_role": body.firm_role.value})
    db.commit()
    return {"user_id": user.id, "firm_role": body.firm_role.value, "api_key": key,
            "note": "store this key now; it is not retrievable later"}


@app.patch("/firms/{firm_id}/members/{user_id}")
def change_member_role(firm_id: str, user_id: str, body: MemberRoleIn,
                       actor: auth.Actor = Depends(any_authenticated),
                       db: Session = Depends(get_db)):
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    m = _membership_or_404(db, firm_id, user_id)
    _assert_can_grant(actor, m.firm_role)
    _assert_can_grant(actor, body.firm_role.value)
    if body.firm_role.value != FirmRole.OWNER.value:
        _assert_not_last_owner(db, m)
    previous, m.firm_role = m.firm_role, body.firm_role.value
    audit.record(db, actor=actor.id, action="firm.member_role_changed", entity_type="firm",
                 entity_id=firm_id, payload={"user_id": user_id, "from": previous,
                                             "to": m.firm_role})
    db.commit()
    return {"user_id": user_id, "firm_role": m.firm_role}


@app.delete("/firms/{firm_id}/members/{user_id}")
def remove_member(firm_id: str, user_id: str, actor: auth.Actor = Depends(any_authenticated),
                  db: Session = Depends(get_db)):
    """Removes firm + project access. A user left with no firms has their keys revoked."""
    auth.assert_firm_access(actor, firm_id, FirmRole.ADMIN)
    m = _membership_or_404(db, firm_id, user_id)
    _assert_can_grant(actor, m.firm_role)
    _assert_not_last_owner(db, m)
    db.delete(m)
    for pm in db.execute(select(ProjectMembership).where(
            ProjectMembership.firm_id == firm_id,
            ProjectMembership.user_id == user_id)).scalars():
        db.delete(pm)
    db.flush()
    # The privileged connection role is required to see memberships in other firms.
    remaining = _count_other_memberships_unscoped(user_id, firm_id)
    if remaining == 0:
        user = db.get(User, user_id)
        user.active = False
        for k in db.execute(select(ApiKey).where(ApiKey.user_id == user_id)).scalars():
            k.active = False
    audit.record(db, actor=actor.id, action="firm.member_removed", entity_type="firm",
                 entity_id=firm_id, payload={"user_id": user_id,
                                             "deactivated": remaining == 0})
    db.commit()
    return {"user_id": user_id, "removed": True, "deactivated": remaining == 0}


def _count_other_memberships_unscoped(user_id: str, firm_id: str) -> int:
    """Separate privileged session: RLS hides the user's other firms from the tenant session."""
    with database.session() as s:
        return s.execute(select(func.count()).select_from(FirmMembership)
                         .where(FirmMembership.user_id == user_id,
                                FirmMembership.firm_id != firm_id)).scalar()


@app.get("/projects/{project_id}/members")
def list_project_members(project_id: str, actor: auth.Actor = Depends(any_authenticated),
                         db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    auth.assert_firm_access(actor, p.firm_id, FirmRole.ADMIN)
    rows = db.execute(select(ProjectMembership, User)
                      .join(User, ProjectMembership.user_id == User.id)
                      .where(ProjectMembership.project_id == p.id)).all()
    return [{"user_id": u.id, "name": u.name, "email": u.email} for _, u in rows]


@app.post("/projects/{project_id}/members", status_code=201)
def grant_project_access(project_id: str, body: ProjectMemberIn,
                         actor: auth.Actor = Depends(any_authenticated),
                         db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    auth.assert_firm_access(actor, p.firm_id, FirmRole.ADMIN)
    _membership_or_404(db, p.firm_id, body.user_id)
    exists = db.execute(select(ProjectMembership.id).where(
        ProjectMembership.project_id == p.id,
        ProjectMembership.user_id == body.user_id)).first()
    if exists is None:
        db.add(ProjectMembership(firm_id=p.firm_id, project_id=p.id, user_id=body.user_id))
        audit.record(db, actor=actor.id, action="project.access_granted",
                     entity_type="project", entity_id=p.id,
                     payload={"user_id": body.user_id})
        db.commit()
    return {"project_id": p.id, "user_id": body.user_id}


@app.delete("/projects/{project_id}/members/{user_id}")
def revoke_project_access(project_id: str, user_id: str,
                          actor: auth.Actor = Depends(any_authenticated),
                          db: Session = Depends(get_db)):
    p = _get_or_404(db, Project, project_id)
    auth.assert_firm_access(actor, p.firm_id, FirmRole.ADMIN)
    pm = db.execute(select(ProjectMembership).where(
        ProjectMembership.project_id == p.id,
        ProjectMembership.user_id == user_id)).scalar_one_or_none()
    if pm is None:
        raise HTTPException(404, "user has no access to this project")
    db.delete(pm)
    audit.record(db, actor=actor.id, action="project.access_revoked", entity_type="project",
                 entity_id=p.id, payload={"user_id": user_id})
    db.commit()
    return {"project_id": p.id, "user_id": user_id, "revoked": True}


@app.get("/users")
def list_users(actor: auth.Actor = Depends(admin_only), db: Session = Depends(get_db)):
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role,
             "firm_id": u.firm_id, "expert_id": u.expert_id, "active": u.active}
            for u in db.execute(select(User).order_by(User.created_at)).scalars()]


@app.get("/experts")
def list_experts(actor: auth.Actor = Depends(internal), db: Session = Depends(get_db)):
    return [_expert_out(e)
            for e in db.execute(select(Expert).order_by(Expert.created_at)).scalars()]


@app.get("/projects/{project_id}/tasks")
def list_project_tasks(project_id: str, actor: auth.Actor = Depends(any_authenticated),
                       db: Session = Depends(get_db)):
    if actor.role not in (ADMIN, OPS, CLIENT):
        raise HTTPException(403, f"role '{actor.role}' is not permitted for this action")
    p = _get_or_404(db, Project, project_id)
    auth.assert_project_access(db, actor, p)
    rows = db.execute(select(Task, Expert).outerjoin(Expert, Task.assigned_to == Expert.id)
                      .where(Task.project_id == p.id).order_by(Task.created_at)).all()
    out = []
    for t, ex in rows:
        item = {"id": t.id, "prompt": t.prompt, "status": t.status,
                "assigned_to": t.assigned_to, "created_at": t.created_at,
                "assigned_expert": {"id": ex.id, "name": ex.name, "email": ex.email,
                                    "credentials": ex.credentials} if ex else None}
        if actor.is_internal:
            item["is_gold"] = t.is_gold
        out.append(item)
    return out


@app.get("/my/tasks")
def my_tasks(actor: auth.Actor = Depends(submit_roles), db: Session = Depends(get_db)):
    if not actor.expert_id:
        return []
    rows = db.execute(
        select(Task, Project).join(Project, Task.project_id == Project.id)
        .where(Task.assigned_to == actor.expert_id).order_by(Task.created_at)
    ).all()
    # is_gold is withheld so honeypot tasks stay indistinguishable to the expert.
    return [{"id": t.id, "project_id": p.id, "project_name": p.name,
             "task_type": p.task_type, "track": p.track, "prompt": t.prompt,
             "context": t.context, "status": t.status, "guidelines": p.guidelines}
            for t, p in rows]


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
<p><a href="/app"><strong>Open the dashboard</strong></a> &middot; <a href="/docs">API documentation</a> &middot; <a href="/healthz">health</a></p>
<p>New deployment? <code>POST /bootstrap</code> creates the first admin user and API key.</p>
</body></html>"""


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    db.execute(select(func.count()).select_from(User))
    return {"ok": True, "version": app.version}


DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


# Some proxies (including the v0 preview) strip trailing slashes while the
# StaticFiles mount redirects /app -> /app/, which loops forever. Serving the
# shell at both paths without a redirect keeps the dashboard reachable either way.
@app.get("/app", include_in_schema=False)
@app.get("/app/", include_in_schema=False)
def dashboard_shell():
    return FileResponse(DASHBOARD_DIR / "index.html", media_type="text/html")


app.mount("/app", StaticFiles(directory=DASHBOARD_DIR), name="dashboard")
