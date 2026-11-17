"""Idempotency-Key middleware."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from factory import idempotency
from factory.api.main import app
from factory.config import settings
from factory.models import Firm, IdempotencyRecord


@pytest.fixture()
def client(db):
    return TestClient(app)


def _firm(name="Acme LLP"):
    return {"name": name, "billing_email": "ap@acme.example.com"}


def _post(client, body, key=None, actor="admin-1"):
    headers = {"X-Actor-Id": actor}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/firms", json=body, headers=headers)


def test_retry_replays_response_without_reexecuting(client, db):
    first = _post(client, _firm(), key="k-1")
    second = _post(client, _firm(), key="k-1")
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert db.query(Firm).count() == 1


def test_without_key_each_request_executes(client, db):
    _post(client, _firm("A"))
    _post(client, _firm("B"))
    assert db.query(Firm).count() == 2


def test_key_reuse_with_different_body_is_rejected(client, db):
    _post(client, _firm("A"), key="k-2")
    r = _post(client, _firm("B"), key="k-2")
    assert r.status_code == 422
    assert db.query(Firm).count() == 1


def test_keys_are_scoped_per_principal(client, db):
    _post(client, _firm("A"), key="shared", actor="tenant-a-admin")
    r = _post(client, _firm("B"), key="shared", actor="tenant-b-admin")
    assert r.status_code == 201 and "idempotent-replayed" not in r.headers
    assert db.query(Firm).count() == 2


def test_deterministic_client_errors_are_replayed(client, db):
    bad = {"name": "X", "billing_email": "not-an-email"}
    first = _post(client, bad, key="k-3")
    second = _post(client, bad, key="k-3")
    assert first.status_code == second.status_code == 422
    assert second.headers["idempotent-replayed"] == "true"


def test_conflict_responses_release_the_key(client, db):
    _post(client, _firm("Dup"))
    first = _post(client, _firm("Dup"), key="k-4")  # unique-name conflict
    assert first.status_code in (400, 409)
    if first.status_code == 409:
        assert db.get(IdempotencyRecord, _record_id("k-4")) is None


def test_invalid_key_is_rejected(client):
    assert _post(client, _firm(), key="x" * 256).status_code == 400
    assert _post(client, _firm(), key="").status_code == 400


def _record_id(key, actor="admin-1"):
    return idempotency._sha256(f"dev:{actor}".encode(), key.encode())


def _seed(db, key, *, age, state=idempotency.IN_PROGRESS, body=None):
    request_hash = idempotency._sha256(b"POST", b"/firms", b"", body or b"")
    db.add(IdempotencyRecord(id=_record_id(key), method="POST", path="/firms",
                             request_hash=request_hash, state=state,
                             status_code=201 if state == idempotency.COMPLETED else None,
                             content_type="application/json", response_body='{"old":true}',
                             created_at=datetime.now(timezone.utc) - age))
    db.commit()


def _raw_body(body):
    import json
    return json.dumps(body, separators=(",", ":")).encode()


def test_concurrent_attempt_gets_409(client, db):
    body = _firm()
    _seed(db, "k-5", age=timedelta(seconds=1), body=_raw_body(body))
    r = client.post("/firms", content=_raw_body(body),
                    headers={"X-Actor-Id": "admin-1", "Idempotency-Key": "k-5",
                             "content-type": "application/json"})
    assert r.status_code == 409 and r.headers["retry-after"] == "1"
    assert db.query(Firm).count() == 0


def test_stale_in_progress_claim_is_taken_over(client, db):
    body = _firm()
    stale = timedelta(seconds=settings.idempotency_lock_seconds + 5)
    _seed(db, "k-6", age=stale, body=_raw_body(body))
    r = client.post("/firms", content=_raw_body(body),
                    headers={"X-Actor-Id": "admin-1", "Idempotency-Key": "k-6",
                             "content-type": "application/json"})
    assert r.status_code == 201
    assert db.query(Firm).count() == 1


def test_expired_record_is_not_replayed(client, db):
    body = _firm()
    _seed(db, "k-7", age=timedelta(hours=settings.idempotency_ttl_hours + 1),
          state=idempotency.COMPLETED, body=_raw_body(body))
    r = client.post("/firms", content=_raw_body(body),
                    headers={"X-Actor-Id": "admin-1", "Idempotency-Key": "k-7",
                             "content-type": "application/json"})
    assert r.status_code == 201 and "idempotent-replayed" not in r.headers
    assert r.json() != {"old": True}


def test_purge_removes_only_expired_records(db):
    _seed(db, "old", age=timedelta(hours=settings.idempotency_ttl_hours + 1))
    _seed(db, "new", age=timedelta(minutes=1))
    assert idempotency.purge_expired() == 1
    assert db.query(IdempotencyRecord).count() == 1


def test_get_requests_bypass_the_middleware(client, db):
    r = client.get("/healthz", headers={"Idempotency-Key": "k-8"})
    assert r.status_code == 200
    assert db.query(IdempotencyRecord).count() == 0


def test_handler_exception_releases_the_key(client, db, monkeypatch):
    from factory.api import main

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "_firm_out", boom)
    failing = TestClient(app, raise_server_exceptions=False)
    r = _post(failing, _firm(), key="k-9")
    assert r.status_code == 500
    assert db.get(IdempotencyRecord, _record_id("k-9")) is None
