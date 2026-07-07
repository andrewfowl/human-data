"""ORM models for the human data factory.

Core entities:
  Expert          — vetted domain contributor (CPA/CFA/analyst), qualification-gated
  Qualification   — passed domain exam granting access to a track
  Project         — client engagement producing one dataset (SFT / preference / eval)
  Task            — one unit of work inside a project; may be a gold (honeypot) task
  Submission      — an expert's answer to a task; moves through the QC pipeline
  Review          — one review record (deterministic checks, autonomous LLM QC, or human)
  AuditEvent      — hash-chained, append-only audit trail
  ExportBatch     — a released dataset artifact with manifest and checksums
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    ADMIN = "admin"        # internal owner: everything, incl. users/keys/firms/billing
    OPS = "ops"            # internal PM: experts, projects, tasks, exports, billing ops
    REVIEWER = "reviewer"  # human QC reviewer (linked to an expert record)
    EXPERT = "expert"      # contributor (linked to an expert record)
    CLIENT = "client"      # firm-side read access, scoped to their firm


class BillingMode(str, enum.Enum):
    EXTERNAL = "external"  # invoiced outside the system; usage/invoices tracked within
    STRIPE = "stripe"      # embedded Stripe billing (customer + invoices + webhook)


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    ISSUED_EXTERNAL = "issued_external"  # external mode: handed to the firm's AP process
    OPEN = "open"                        # stripe mode: pushed to Stripe, awaiting payment
    PAID = "paid"
    VOID = "void"


class DomainTrack(str, enum.Enum):
    FINANCIAL_ACCOUNTING = "financial_accounting"   # GAAP/IFRS reporting, journal entries
    AUDIT_ASSURANCE = "audit_assurance"             # audit procedures, internal control
    TAX = "tax"                                     # corporate & individual taxation
    FINANCIAL_ANALYSIS = "financial_analysis"       # valuation, modeling, ratios
    MANAGERIAL_ACCOUNTING = "managerial_accounting" # costing, budgeting, variance


class TaskType(str, enum.Enum):
    SFT = "sft"                 # prompt → expert-written completion
    PREFERENCE = "preference"   # prompt → chosen vs rejected responses
    EVAL = "eval"               # question + reference answer + grading rubric


class ExpertStatus(str, enum.Enum):
    PENDING = "pending"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"


class TaskStatus(str, enum.Enum):
    OPEN = "open"
    ASSIGNED = "assigned"
    COMPLETED = "completed"


class SubmissionStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    AUTO_CHECK_FAILED = "auto_check_failed"   # deterministic validators found hard failures
    AUTO_QC_PENDING = "auto_qc_pending"       # awaiting autonomous LLM review
    HUMAN_REVIEW = "human_review"             # sampled or gray-zone: human reviewer required
    NEEDS_REVISION = "needs_revision"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewKind(str, enum.Enum):
    DETERMINISTIC = "deterministic"
    AUTO_LLM = "auto_llm"
    HUMAN = "human"
    GOLD_CHECK = "gold_check"


class Verdict(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    REVISE = "revise"


class Firm(Base):
    """A client organization buying datasets (the billable party)."""

    __tablename__ = "firms"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    billing_email: Mapped[str] = mapped_column(String, nullable=False)
    billing_mode: Mapped[str] = mapped_column(String, default=BillingMode.EXTERNAL.value)
    currency: Mapped[str] = mapped_column(String, default="usd")
    external_reference: Mapped[str] = mapped_column(String, default="")  # PO / AP account no.
    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """An authenticated principal (internal staff or firm-side client user)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    firm_id: Mapped[str | None] = mapped_column(ForeignKey("firms.id"), nullable=True)
    expert_id: Mapped[str | None] = mapped_column(ForeignKey("experts.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    """Bearer credential. Only the SHA-256 of the key is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    prefix: Mapped[str] = mapped_column(String, nullable=False)  # display hint, e.g. hdf_a1b2
    key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Expert(Base):
    __tablename__ = "experts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    credentials: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["CPA", "CFA L3"]
    status: Mapped[str] = mapped_column(String, default=ExpertStatus.PENDING.value)
    is_reviewer: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_score: Mapped[float] = mapped_column(Float, default=3.5)  # 0–5 EWMA
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    gold_pass_count: Mapped[int] = mapped_column(Integer, default=0)
    gold_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    qualifications: Mapped[list["Qualification"]] = relationship(back_populates="expert")


class Qualification(Base):
    __tablename__ = "qualifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    expert_id: Mapped[str] = mapped_column(ForeignKey("experts.id"), nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    exam_score: Mapped[float] = mapped_column(Float, nullable=False)  # 0–100
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    expert: Mapped[Expert] = relationship(back_populates="qualifications")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firms.id"), nullable=False)
    track: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    guidelines: Mapped[str] = mapped_column(Text, default="")
    rubric_id: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)  # actor id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tasks: Mapped[list["Task"]] = relationship(back_populates="project")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # source docs, figures, constraints
    is_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    gold_answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # never shown to experts
    status: Mapped[str] = mapped_column(String, default=TaskStatus.OPEN.value)
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("experts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="tasks")
    submissions: Mapped[list["Submission"]] = relationship(back_populates="task")


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    expert_id: Mapped[str] = mapped_column(ForeignKey("experts.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)  # shape depends on task type
    status: Mapped[str] = mapped_column(String, default=SubmissionStatus.SUBMITTED.value)
    sampled_for_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    task: Mapped[Task] = relationship(back_populates="submissions")
    reviews: Mapped[list["Review"]] = relationship(back_populates="submission")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String, nullable=False)  # expert id or engine id
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0–5
    detail: Mapped[dict] = mapped_column(JSON, default=dict)  # criterion scores, findings, flags
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    submission: Mapped[Submission] = relationship(back_populates="reviews")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True, default=_id)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RateCard(Base):
    """Per-firm price per approved record, by task type. Firm-less rows are defaults."""

    __tablename__ = "rate_cards"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    firm_id: Mapped[str | None] = mapped_column(ForeignKey("firms.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageEvent(Base):
    """One billable unit. Emitted when a (non-gold) record is approved.

    `task_id` is unique per kind so a task can never be billed twice, even if a
    revised submission is approved later.
    """

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firms.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, default="approved_record")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, default="usd")
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    firm_id: Mapped[str] = mapped_column(ForeignKey("firms.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)  # billing mode at issuance
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String, default="usd")
    status: Mapped[str] = mapped_column(String, default=InvoiceStatus.DRAFT.value)
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    stripe_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True)
    hosted_invoice_url: Mapped[str | None] = mapped_column(String, nullable=True)
    external_paid_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    issued_by: Mapped[str] = mapped_column(String, nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExportBatch(Base):
    __tablename__ = "export_batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)  # dual control
    path: Mapped[str | None] = mapped_column(String, nullable=True)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending_approval")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
