import pytest
from sqlalchemy.exc import IntegrityError

from factory import audit
from factory.models import AuditEvent


def test_chain_links_and_verifies(db):
    a = audit.record(db, actor="x", action="a", entity_type="t", entity_id="1")
    b = audit.record(db, actor="x", action="b", entity_type="t", entity_id="1")
    db.commit()
    assert a.prev_hash == audit.GENESIS
    assert b.prev_hash == a.hash
    assert audit.verify_chain(db) == (True, 2)


def test_fork_is_rejected_by_unique_prev_hash(db):
    # Simulates the race: a second writer that read the same head.
    head = audit.record(db, actor="x", action="a", entity_type="t", entity_id="1")
    db.commit()
    db.add(AuditEvent(actor="y", action="fork", entity_type="t", entity_id="1",
                      payload={}, prev_hash=head.prev_hash, hash="f" * 64))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert audit.verify_chain(db) == (True, 1)


def test_tamper_detected(db):
    audit.record(db, actor="x", action="a", entity_type="t", entity_id="1")
    ev = audit.record(db, actor="x", action="b", entity_type="t", entity_id="1")
    db.commit()
    ev.payload = {"edited": True}
    db.commit()
    assert audit.verify_chain(db) == (False, ev.seq)
