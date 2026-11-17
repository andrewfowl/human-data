"""Hash-chained, append-only audit trail.

Every state transition in the factory is recorded as an AuditEvent whose hash
covers the previous event's hash — tampering with any historical record breaks
the chain and is detectable by `verify_chain`. This is the evidentiary backbone
for the internal-control system (SOC-style traceability for dataset provenance).
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import AuditEvent

GENESIS = "0" * 64

# Arbitrary fixed key for the transaction-scoped advisory lock that serializes
# appends. Without it, two concurrent writers read the same head and fork the
# chain; UNIQUE(prev_hash) is the backstop that turns any fork into an error.
AUDIT_LOCK_KEY = 0x48444641554454  # "HDFAUDT"


def _lock_chain(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK_KEY})


def _event_hash(prev_hash: str, actor: str, action: str, entity_type: str,
                entity_id: str, payload: dict) -> str:
    body = json.dumps(
        {"prev": prev_hash, "actor": actor, "action": action,
         "entity_type": entity_type, "entity_id": entity_id, "payload": payload},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def record(db: Session, *, actor: str, action: str, entity_type: str,
           entity_id: str, payload: dict | None = None) -> AuditEvent:
    payload = payload or {}
    _lock_chain(db)
    last = db.execute(
        select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = last.hash if last else GENESIS
    ev = AuditEvent(
        actor=actor, action=action, entity_type=entity_type, entity_id=entity_id,
        payload=payload, prev_hash=prev_hash,
        hash=_event_hash(prev_hash, actor, action, entity_type, entity_id, payload),
    )
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session) -> tuple[bool, int]:
    """Walk the full chain; returns (intact, events_checked)."""
    events = db.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
    prev = GENESIS
    for ev in events:
        expected = _event_hash(prev, ev.actor, ev.action, ev.entity_type, ev.entity_id, ev.payload)
        if ev.prev_hash != prev or ev.hash != expected:
            return False, ev.seq
        prev = ev.hash
    return True, len(events)


def chain_head(db: Session) -> str:
    last = db.execute(
        select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    return last.hash if last else GENESIS
