"""Auth-enabled behavior: bootstrap, bearer keys, RBAC, firm scoping."""

import pytest
from fastapi.testclient import TestClient

from factory.api.main import app
from factory.config import settings


@pytest.fixture()
def authed_client(db):
    settings.auth_disabled = False
    with TestClient(app) as c:
        yield c
    settings.auth_disabled = True


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_unauthenticated_requests_rejected(authed_client):
    assert authed_client.get("/metrics").status_code == 401
    assert authed_client.get("/firms").status_code == 401
    r = authed_client.get("/metrics", headers=_bearer("hdf_not_a_real_key"))
    assert r.status_code == 401


def test_bootstrap_once_then_locked(authed_client):
    r = authed_client.post("/bootstrap", json={"name": "Owner", "email": "owner@example.com"})
    assert r.status_code == 201
    key = r.json()["api_key"]
    assert key.startswith("hdf_")

    # second bootstrap attempt is rejected
    r = authed_client.post("/bootstrap", json={"name": "X", "email": "x@example.com"})
    assert r.status_code == 409

    # the issued key works
    r = authed_client.get("/metrics", headers=_bearer(key))
    assert r.status_code == 200


def test_rbac_and_firm_scoping(authed_client):
    admin_key = authed_client.post(
        "/bootstrap", json={"name": "Owner", "email": "owner@example.com"}
    ).json()["api_key"]
    admin = _bearer(admin_key)

    firm_a = authed_client.post("/firms", json={
        "name": "Firm A", "billing_email": "ap@a.example.com"}, headers=admin).json()
    firm_b = authed_client.post("/firms", json={
        "name": "Firm B", "billing_email": "ap@b.example.com"}, headers=admin).json()

    client_key = authed_client.post("/users", json={
        "name": "Client A", "email": "client@a.example.com", "role": "client",
        "firm_id": firm_a["id"]}, headers=admin).json()["api_key"]
    client_hdr = _bearer(client_key)

    # client can see their own firm & billing, not another firm's
    assert authed_client.get(f"/firms/{firm_a['id']}", headers=client_hdr).status_code == 200
    assert authed_client.get(f"/firms/{firm_b['id']}", headers=client_hdr).status_code == 403
    assert authed_client.get(f"/firms/{firm_a['id']}/billing", headers=client_hdr).status_code == 200
    assert authed_client.get(f"/firms/{firm_b['id']}/invoices", headers=client_hdr).status_code == 403

    # client cannot perform internal actions
    r = authed_client.post("/experts", json={"name": "E", "email": "e@example.com"},
                           headers=client_hdr)
    assert r.status_code == 403
    r = authed_client.post("/firms", json={"name": "F", "billing_email": "f@example.com"},
                           headers=client_hdr)
    assert r.status_code == 403

    # revocation kills the key
    resp = authed_client.post("/users", json={
        "name": "Client A2", "email": "client2@a.example.com", "role": "client",
        "firm_id": firm_a["id"]}, headers=admin).json()
    key2 = resp["api_key"]
    assert authed_client.get(f"/firms/{firm_a['id']}", headers=_bearer(key2)).status_code == 200
    authed_client.post(f"/users/{resp['user_id']}/revoke", headers=admin)
    assert authed_client.get(f"/firms/{firm_a['id']}", headers=_bearer(key2)).status_code == 401


def test_client_user_requires_firm(authed_client):
    admin_key = authed_client.post(
        "/bootstrap", json={"name": "Owner", "email": "owner@example.com"}
    ).json()["api_key"]
    r = authed_client.post("/users", json={
        "name": "C", "email": "c@example.com", "role": "client"},
        headers=_bearer(admin_key))
    assert r.status_code == 422
