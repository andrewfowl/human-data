"""Billing service.

Two billing modes per firm:

  external — the firm is invoiced outside the system (their AP process, wire,
             netting agreement, whatever). The factory still meters every
             billable unit, generates invoice records with line items, and
             tracks payment state (`issued_external` → `paid` via
             record_external_payment), so finance has full visibility inside
             the app even though money moves outside it.

  stripe   — embedded billing: the firm is a Stripe customer; invoice
             generation pushes line items + a send_invoice Stripe invoice, and
             the webhook flips the invoice to paid/void on Stripe events.

Metering: one usage event per approved non-gold task (unique per task — a task
revised and re-approved is never double-billed). Prices resolve from the
firm's rate card, falling back to configured defaults.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..models import (
    BillingMode, Firm, Invoice, InvoiceStatus, Project, RateCard, Submission,
    Task, UsageEvent,
)
from .stripe_gateway import get_gateway


class BillingError(Exception):
    pass


def resolve_unit_price_cents(db: Session, firm_id: str, task_type: str) -> int:
    for scope in (firm_id, None):
        row = db.execute(
            select(RateCard).where(
                RateCard.firm_id == scope,
                RateCard.task_type == task_type,
                RateCard.active.is_(True),
            ).order_by(RateCard.created_at.desc())
        ).scalars().first()
        if row:
            return row.unit_price_cents
    return settings.default_rates_cents[task_type]


def record_approved_record(db: Session, *, project: Project, task: Task,
                           submission: Submission) -> UsageEvent | None:
    """Meter one approved record. Idempotent per task; gold tasks are free."""
    if task.is_gold:
        return None
    existing = db.execute(
        select(UsageEvent).where(UsageEvent.task_id == task.id,
                                 UsageEvent.kind == "approved_record")
    ).scalar_one_or_none()
    if existing:
        return None
    firm: Firm = db.get(Firm, project.firm_id)
    price = resolve_unit_price_cents(db, firm.id, project.task_type)
    ev = UsageEvent(
        firm_id=firm.id, project_id=project.id, task_id=task.id,
        submission_id=submission.id, kind="approved_record",
        quantity=1, unit_price_cents=price, amount_cents=price,
        currency=firm.currency,
    )
    db.add(ev)
    db.flush()
    audit.record(db, actor="billing-engine", action="usage.recorded",
                 entity_type="usage_event", entity_id=ev.id,
                 payload={"firm_id": firm.id, "task_id": task.id,
                          "amount_cents": price})
    return ev


def _next_invoice_number(db: Session, when: datetime) -> str:
    count = db.execute(select(func.count()).select_from(Invoice)).scalar() or 0
    return f"HDF-{when:%Y%m}-{count + 1:04d}"


def generate_invoice(db: Session, *, firm: Firm, period_start: datetime,
                     period_end: datetime, issued_by: str) -> Invoice:
    """Roll all uninvoiced usage in the period into one invoice and issue it."""
    events = db.execute(
        select(UsageEvent).where(
            UsageEvent.firm_id == firm.id,
            UsageEvent.invoice_id.is_(None),
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        ).order_by(UsageEvent.created_at)
    ).scalars().all()
    if not events:
        raise BillingError("no uninvoiced usage in this period")

    # Aggregate line items by project + kind + unit price.
    grouped: dict[tuple, dict] = {}
    for ev in events:
        key = (ev.project_id, ev.kind, ev.unit_price_cents)
        row = grouped.setdefault(key, {
            "project_id": ev.project_id, "kind": ev.kind,
            "unit_price_cents": ev.unit_price_cents, "quantity": 0, "amount_cents": 0,
        })
        row["quantity"] += ev.quantity
        row["amount_cents"] += ev.amount_cents
    line_items = []
    for row in grouped.values():
        project = db.get(Project, row["project_id"])
        row["description"] = (
            f"{project.name} — {row['quantity']} approved {project.task_type} record(s) "
            f"@ {row['unit_price_cents'] / 100:.2f} {firm.currency.upper()}"
        )
        line_items.append(row)

    now = datetime.now(timezone.utc)
    invoice = Invoice(
        number=_next_invoice_number(db, now),
        firm_id=firm.id, mode=firm.billing_mode,
        period_start=period_start, period_end=period_end,
        subtotal_cents=sum(r["amount_cents"] for r in line_items),
        currency=firm.currency, line_items=line_items, issued_by=issued_by,
    )
    db.add(invoice)
    db.flush()
    for ev in events:
        ev.invoice_id = invoice.id

    if firm.billing_mode == BillingMode.STRIPE.value:
        gateway = get_gateway()
        firm.stripe_customer_id = gateway.ensure_customer(firm)
        stripe_id, url = gateway.push_invoice(firm, invoice)
        invoice.stripe_invoice_id = stripe_id
        invoice.hosted_invoice_url = url
        invoice.status = InvoiceStatus.OPEN.value
    else:
        invoice.status = InvoiceStatus.ISSUED_EXTERNAL.value
    invoice.issued_at = now

    audit.record(db, actor=issued_by, action="invoice.issued",
                 entity_type="invoice", entity_id=invoice.id,
                 payload={"number": invoice.number, "firm_id": firm.id,
                          "mode": invoice.mode, "subtotal_cents": invoice.subtotal_cents,
                          "usage_events": len(events)})
    db.commit()
    return invoice


def record_external_payment(db: Session, *, invoice: Invoice, reference: str,
                            actor: str) -> Invoice:
    """Mark an externally-billed invoice as settled (with the AP reference)."""
    if invoice.mode != BillingMode.EXTERNAL.value:
        raise BillingError("invoice is Stripe-managed; payment arrives via webhook")
    if invoice.status not in (InvoiceStatus.ISSUED_EXTERNAL.value, InvoiceStatus.DRAFT.value):
        raise BillingError(f"invoice is in status '{invoice.status}'")
    invoice.status = InvoiceStatus.PAID.value
    invoice.external_paid_reference = reference
    invoice.paid_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor, action="invoice.external_payment_recorded",
                 entity_type="invoice", entity_id=invoice.id,
                 payload={"reference": reference})
    db.commit()
    return invoice


PAYABLE_STATUSES = (InvoiceStatus.ISSUED_EXTERNAL.value, InvoiceStatus.OPEN.value)


def create_checkout(db: Session, *, invoice: Invoice, firm: Firm,
                    success_url: str, cancel_url: str, actor: str) -> tuple[Invoice, str]:
    """Create a self-serve Stripe Checkout session for an unpaid invoice.

    Works for both billing modes: stripe-mode invoices get a card alternative
    to the hosted Stripe invoice, and external-mode firms can settle by card
    instead of their AP process. Payment lands via the
    `checkout.session.completed` webhook.
    """
    if invoice.status not in PAYABLE_STATUSES:
        raise BillingError(f"invoice is in status '{invoice.status}', not payable")
    for url in (success_url, cancel_url):
        if not url.startswith(("https://", "http://")):
            raise BillingError("success_url and cancel_url must be absolute URLs")
    gateway = get_gateway()
    session_id, url = gateway.create_checkout_session(firm, invoice, success_url, cancel_url)
    invoice.checkout_session_id = session_id
    audit.record(db, actor=actor, action="invoice.checkout_created",
                 entity_type="invoice", entity_id=invoice.id,
                 payload={"checkout_session_id": session_id})
    db.commit()
    return invoice, url


def handle_stripe_event(db: Session, event: dict) -> dict:
    """Apply a Stripe webhook event to the matching invoice."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    # Self-serve checkout completion — matched by our metadata, then by session id.
    if etype == "checkout.session.completed":
        invoice_id = (obj.get("metadata") or {}).get("hdf_invoice_id")
        invoice = db.get(Invoice, invoice_id) if invoice_id else None
        if invoice is None and obj.get("id"):
            invoice = db.execute(
                select(Invoice).where(Invoice.checkout_session_id == obj["id"])
            ).scalar_one_or_none()
        if invoice is None:
            return {"handled": False, "reason": "unknown checkout session"}
        if invoice.status == InvoiceStatus.PAID.value:
            return {"handled": True, "invoice_id": invoice.id, "status": invoice.status}
        invoice.status = InvoiceStatus.PAID.value
        invoice.paid_at = datetime.now(timezone.utc)
        reference = obj.get("payment_intent") or obj.get("id") or ""
        if invoice.mode == BillingMode.EXTERNAL.value:
            invoice.external_paid_reference = f"stripe-checkout:{reference}"
        audit.record(db, actor="stripe-webhook", action="invoice.paid",
                     entity_type="invoice", entity_id=invoice.id,
                     payload={"stripe_event": etype, "reference": reference})
        db.commit()
        return {"handled": True, "invoice_id": invoice.id, "status": invoice.status}

    stripe_invoice_id = obj.get("id")
    if not stripe_invoice_id or not etype.startswith("invoice."):
        return {"handled": False, "reason": "not an invoice event"}
    invoice = db.execute(
        select(Invoice).where(Invoice.stripe_invoice_id == stripe_invoice_id)
    ).scalar_one_or_none()
    if invoice is None:
        return {"handled": False, "reason": "unknown invoice"}

    if etype in ("invoice.paid", "invoice.payment_succeeded"):
        invoice.status = InvoiceStatus.PAID.value
        invoice.paid_at = datetime.now(timezone.utc)
    elif etype == "invoice.voided":
        invoice.status = InvoiceStatus.VOID.value
    else:
        return {"handled": False, "reason": f"ignored event type {etype}"}

    audit.record(db, actor="stripe-webhook", action=f"invoice.{invoice.status}",
                 entity_type="invoice", entity_id=invoice.id,
                 payload={"stripe_event": etype, "stripe_invoice_id": stripe_invoice_id})
    db.commit()
    return {"handled": True, "invoice_id": invoice.id, "status": invoice.status}


def reconciliation_report(db: Session, firm: Firm,
                          period_start: datetime | None = None,
                          period_end: datetime | None = None) -> dict:
    """Three-way tie-out for a firm: metered usage vs invoiced vs settled.

    `clean` is true when every metered cent in the window is on an invoice and
    every issued invoice is either awaiting payment or paid — i.e. no leakage
    between production, invoicing, and cash.
    """
    uq = select(UsageEvent).where(UsageEvent.firm_id == firm.id)
    iq = select(Invoice).where(Invoice.firm_id == firm.id)
    if period_start:
        uq = uq.where(UsageEvent.created_at >= period_start)
        iq = iq.where(Invoice.created_at >= period_start)
    if period_end:
        uq = uq.where(UsageEvent.created_at < period_end)
        iq = iq.where(Invoice.created_at < period_end)
    events = db.execute(uq).scalars().all()
    invoices = db.execute(iq).scalars().all()

    metered = sum(e.amount_cents for e in events)
    metered_invoiced = sum(e.amount_cents for e in events if e.invoice_id)
    metered_uninvoiced = metered - metered_invoiced
    issued = sum(i.subtotal_cents for i in invoices
                 if i.status != InvoiceStatus.VOID.value)
    paid = sum(i.subtotal_cents for i in invoices
               if i.status == InvoiceStatus.PAID.value)
    outstanding = sum(i.subtotal_cents for i in invoices if i.status in (
        InvoiceStatus.ISSUED_EXTERNAL.value, InvoiceStatus.OPEN.value))
    voided = sum(i.subtotal_cents for i in invoices
                 if i.status == InvoiceStatus.VOID.value)

    per_project: dict[str, dict] = {}
    for e in events:
        row = per_project.setdefault(e.project_id, {
            "project_id": e.project_id, "metered_cents": 0,
            "invoiced_cents": 0, "uninvoiced_cents": 0, "records": 0,
        })
        row["metered_cents"] += e.amount_cents
        row["records"] += e.quantity
        if e.invoice_id:
            row["invoiced_cents"] += e.amount_cents
        else:
            row["uninvoiced_cents"] += e.amount_cents
    for row in per_project.values():
        project = db.get(Project, row["project_id"])
        row["project_name"] = project.name if project else None

    return {
        "firm_id": firm.id,
        "firm_name": firm.name,
        "billing_mode": firm.billing_mode,
        "currency": firm.currency,
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "metered_cents": metered,
        "metered_invoiced_cents": metered_invoiced,
        "metered_uninvoiced_cents": metered_uninvoiced,
        "invoiced_cents": issued,
        "paid_cents": paid,
        "outstanding_cents": outstanding,
        "voided_cents": voided,
        "invoiced_not_paid_cents": issued - paid,
        "per_project": sorted(per_project.values(), key=lambda r: r["project_id"]),
        "invoices": [{"number": i.number, "status": i.status,
                      "subtotal_cents": i.subtotal_cents} for i in invoices],
        "clean": metered_uninvoiced == 0 and metered_invoiced == issued,
    }


def firm_billing_summary(db: Session, firm: Firm) -> dict:
    uninvoiced = db.execute(
        select(func.coalesce(func.sum(UsageEvent.amount_cents), 0), func.count())
        .where(UsageEvent.firm_id == firm.id, UsageEvent.invoice_id.is_(None))
    ).one()
    invoiced = db.execute(
        select(Invoice.status, func.coalesce(func.sum(Invoice.subtotal_cents), 0),
               func.count())
        .where(Invoice.firm_id == firm.id).group_by(Invoice.status)
    ).all()
    return {
        "firm_id": firm.id,
        "billing_mode": firm.billing_mode,
        "currency": firm.currency,
        "uninvoiced": {"amount_cents": int(uninvoiced[0]), "events": uninvoiced[1]},
        "invoices_by_status": {
            status: {"amount_cents": int(amount), "count": count}
            for status, amount, count in invoiced
        },
        "outstanding_cents": sum(
            int(amount) for status, amount, _ in invoiced
            if status in (InvoiceStatus.ISSUED_EXTERNAL.value, InvoiceStatus.OPEN.value)
        ),
    }
