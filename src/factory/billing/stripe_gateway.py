"""Stripe integration behind a small gateway interface.

`LiveStripeGateway` talks to Stripe (requires STRIPE_SECRET_KEY); the fake
gateway is used automatically when no key is configured, so the whole billing
flow — including "stripe"-mode firms — is exercisable offline and in tests.
Webhook signatures are verified in live mode; in fake mode webhooks are only
accepted when auth is disabled (dev/tests), never in production.
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol

from ..config import settings
from ..models import Firm, Invoice


class StripeGateway(Protocol):
    def ensure_customer(self, firm: Firm) -> str: ...
    def push_invoice(self, firm: Firm, invoice: Invoice) -> tuple[str, str]: ...
    def create_checkout_session(self, firm: Firm, invoice: Invoice,
                                success_url: str, cancel_url: str) -> tuple[str, str]: ...
    def parse_webhook(self, payload: bytes, signature: str | None) -> dict: ...


class LiveStripeGateway:
    def __init__(self) -> None:
        import stripe

        stripe.api_key = settings.stripe_secret_key
        self._stripe = stripe

    def ensure_customer(self, firm: Firm) -> str:
        if firm.stripe_customer_id:
            return firm.stripe_customer_id
        customer = self._stripe.Customer.create(
            name=firm.name, email=firm.billing_email,
            metadata={"hdf_firm_id": firm.id},
        )
        return customer.id

    def push_invoice(self, firm: Firm, invoice: Invoice) -> tuple[str, str]:
        customer_id = firm.stripe_customer_id
        for item in invoice.line_items:
            self._stripe.InvoiceItem.create(
                customer=customer_id,
                amount=item["amount_cents"],
                currency=invoice.currency,
                description=item["description"],
                metadata={"hdf_invoice_id": invoice.id},
            )
        st_invoice = self._stripe.Invoice.create(
            customer=customer_id,
            collection_method="send_invoice",
            days_until_due=30,
            metadata={"hdf_invoice_id": invoice.id, "hdf_invoice_number": invoice.number},
        )
        st_invoice = self._stripe.Invoice.finalize_invoice(st_invoice.id)
        self._stripe.Invoice.send_invoice(st_invoice.id)
        return st_invoice.id, st_invoice.hosted_invoice_url or ""

    def create_checkout_session(self, firm: Firm, invoice: Invoice,
                                success_url: str, cancel_url: str) -> tuple[str, str]:
        session = self._stripe.checkout.Session.create(
            mode="payment",
            customer=firm.stripe_customer_id or None,
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": invoice.currency,
                    "unit_amount": invoice.subtotal_cents,
                    "product_data": {
                        "name": f"Invoice {invoice.number}",
                        "description": f"Human Data Factory — {firm.name}",
                    },
                },
            }],
            metadata={"hdf_invoice_id": invoice.id, "hdf_invoice_number": invoice.number},
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return session.id, session.url or ""

    def parse_webhook(self, payload: bytes, signature: str | None) -> dict:
        if not settings.stripe_webhook_secret:
            raise PermissionError("STRIPE_WEBHOOK_SECRET is not configured")
        event = self._stripe.Webhook.construct_event(
            payload, signature or "", settings.stripe_webhook_secret
        )
        return event.to_dict() if hasattr(event, "to_dict") else dict(event)


class FakeStripeGateway:
    """Deterministic offline stand-in. Never used for real money movement."""

    def ensure_customer(self, firm: Firm) -> str:
        return firm.stripe_customer_id or f"cus_fake_{firm.id[:12]}"

    def push_invoice(self, firm: Firm, invoice: Invoice) -> tuple[str, str]:
        fake_id = f"in_fake_{uuid.uuid4().hex[:16]}"
        return fake_id, f"https://invoice.example/{fake_id}"

    def create_checkout_session(self, firm: Firm, invoice: Invoice,
                                success_url: str, cancel_url: str) -> tuple[str, str]:
        fake_id = f"cs_fake_{uuid.uuid4().hex[:16]}"
        return fake_id, f"https://checkout.example/{fake_id}"

    def parse_webhook(self, payload: bytes, signature: str | None) -> dict:
        if not settings.auth_disabled:
            # Without a webhook secret there is no way to authenticate the caller.
            raise PermissionError("stripe webhooks require STRIPE_WEBHOOK_SECRET in production")
        return json.loads(payload)


def get_gateway() -> StripeGateway:
    if settings.stripe_secret_key:
        return LiveStripeGateway()
    return FakeStripeGateway()
