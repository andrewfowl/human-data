import json
from datetime import datetime, timedelta, timezone

import pytest

from factory.billing import service as billing
from factory.config import settings
from factory.models import (
    BillingMode, Firm, InvoiceStatus, RateCard, Task, UsageEvent,
)
from factory.qc import engine
from tests.conftest import GOOD_SFT


def _approve(db, task, author, reviewer):
    sub = engine.submit(db, task=task, expert=author, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass")
    return sub


WIDE_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
WIDE_END = datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_usage_recorded_on_approval_at_default_rate(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    events = db.query(UsageEvent).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.firm_id == firm.id
    assert ev.amount_cents == settings.default_rates_cents["sft"]
    assert ev.invoice_id is None


def test_firm_rate_card_overrides_default(db, firm, project, qualified_expert, reviewer):
    db.add(RateCard(firm_id=firm.id, task_type="sft", unit_price_cents=9900))
    db.commit()
    t = Task(project_id=project.id, prompt="Another task prompt")
    db.add(t)
    db.commit()
    _approve(db, t, qualified_expert, reviewer)
    ev = db.query(UsageEvent).one()
    assert ev.unit_price_cents == 9900


def test_task_never_billed_twice(db, firm, project, task, qualified_expert, reviewer):
    sub = _approve(db, task, qualified_expert, reviewer)
    # simulate a second approval attempt on the same task
    billing.record_approved_record(db, project=project, task=task, submission=sub)
    assert db.query(UsageEvent).count() == 1


def test_gold_tasks_are_free(db, firm, project, qualified_expert):
    gold = Task(project_id=project.id, prompt="gold", is_gold=True,
                gold_answer={"must_include": ["allocation"]})
    db.add(gold)
    db.commit()
    engine.submit(db, task=gold, expert=qualified_expert, content={
        "response": "The allocation follows relative standalone selling price. " + "x " * 150,
        "citations": ["ASC 606"],
    })
    assert db.query(UsageEvent).count() == 0


def test_external_invoice_lifecycle(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    assert inv.status == InvoiceStatus.ISSUED_EXTERNAL.value
    assert inv.subtotal_cents == settings.default_rates_cents["sft"]
    assert inv.number.startswith("HDF-")
    assert len(inv.line_items) == 1
    # usage now attached to the invoice
    ev = db.query(UsageEvent).one()
    assert ev.invoice_id == inv.id

    # cannot invoice the same usage twice
    with pytest.raises(billing.BillingError):
        billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                 period_end=WIDE_END, issued_by="ops")

    inv = billing.record_external_payment(db, invoice=inv, reference="WIRE-778",
                                          actor="ops")
    assert inv.status == InvoiceStatus.PAID.value
    assert inv.external_paid_reference == "WIRE-778"


def test_stripe_mode_uses_gateway_and_webhook_marks_paid(db, task, qualified_expert, reviewer):
    # switch the project's firm to stripe mode
    firm = db.query(Firm).one()
    firm.billing_mode = BillingMode.STRIPE.value
    db.commit()

    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    assert inv.status == InvoiceStatus.OPEN.value
    assert inv.stripe_invoice_id and inv.stripe_invoice_id.startswith("in_fake_")
    assert firm.stripe_customer_id  # customer created via gateway

    event = {"type": "invoice.paid", "data": {"object": {"id": inv.stripe_invoice_id}}}
    result = billing.handle_stripe_event(db, event)
    assert result["handled"] is True
    db.refresh(inv)
    assert inv.status == InvoiceStatus.PAID.value


def test_external_payment_cannot_be_recorded_twice(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    billing.record_external_payment(db, invoice=inv, reference="WIRE-1", actor="ops")
    with pytest.raises(billing.BillingError):
        billing.record_external_payment(db, invoice=inv, reference="WIRE-2", actor="ops")


def test_billing_summary(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    summary = billing.firm_billing_summary(db, firm)
    assert summary["uninvoiced"]["events"] == 1
    assert summary["uninvoiced"]["amount_cents"] == settings.default_rates_cents["sft"]

    billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                             period_end=WIDE_END, issued_by="ops")
    summary = billing.firm_billing_summary(db, firm)
    assert summary["uninvoiced"]["events"] == 0
    assert summary["outstanding_cents"] == settings.default_rates_cents["sft"]


def test_reconciliation_report(db, firm, project, task, qualified_expert, reviewer):
    # one approved+invoiced task, one approved+uninvoiced task
    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    t2 = Task(project_id=project.id, prompt="A second task prompt for recon")
    db.add(t2)
    db.commit()
    sub2 = engine.submit(db, task=t2, expert=qualified_expert, content=dict(
        GOOD_SFT, response=GOOD_SFT["response"] + " Additional distinct analysis text."))
    engine.human_review(db, submission=sub2, reviewer=reviewer, verdict="pass")

    rate = settings.default_rates_cents["sft"]
    r = billing.reconciliation_report(db, firm)
    assert r["metered_cents"] == 2 * rate
    assert r["metered_invoiced_cents"] == rate
    assert r["metered_uninvoiced_cents"] == rate
    assert r["invoiced_cents"] == rate
    assert r["paid_cents"] == 0
    assert r["outstanding_cents"] == rate
    assert r["clean"] is False  # uninvoiced usage exists

    billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                             period_end=WIDE_END, issued_by="ops")
    billing.record_external_payment(db, invoice=inv, reference="WIRE-9", actor="ops")
    r = billing.reconciliation_report(db, firm)
    assert r["metered_uninvoiced_cents"] == 0
    assert r["paid_cents"] == rate
    assert r["clean"] is True
    assert len(r["per_project"]) == 1
    assert r["per_project"][0]["records"] == 2


def test_checkout_selfserve_payment_flow(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    assert inv.status == InvoiceStatus.ISSUED_EXTERNAL.value

    inv, url = billing.create_checkout(db, invoice=inv, firm=firm,
                                       success_url="https://app.example/paid",
                                       cancel_url="https://app.example/cancel",
                                       actor="client-user")
    assert inv.checkout_session_id.startswith("cs_fake_")
    assert url.startswith("https://checkout.example/")

    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": inv.checkout_session_id,
                                 "payment_intent": "pi_fake_123",
                                 "metadata": {"hdf_invoice_id": inv.id}}}}
    result = billing.handle_stripe_event(db, event)
    assert result["handled"] is True
    db.refresh(inv)
    assert inv.status == InvoiceStatus.PAID.value
    assert inv.external_paid_reference == "stripe-checkout:pi_fake_123"

    # replayed webhook is idempotent
    assert billing.handle_stripe_event(db, event)["handled"] is True
    # a paid invoice cannot open a new checkout
    with pytest.raises(billing.BillingError):
        billing.create_checkout(db, invoice=inv, firm=firm,
                                success_url="https://x.example/s",
                                cancel_url="https://x.example/c", actor="client-user")


def test_checkout_rejects_relative_urls(db, firm, task, qualified_expert, reviewer):
    _approve(db, task, qualified_expert, reviewer)
    inv = billing.generate_invoice(db, firm=firm, period_start=WIDE_START,
                                   period_end=WIDE_END, issued_by="ops")
    with pytest.raises(billing.BillingError):
        billing.create_checkout(db, invoice=inv, firm=firm,
                                success_url="/paid", cancel_url="/cancel", actor="x")
