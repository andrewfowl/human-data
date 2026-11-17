"""Firm tenancy: memberships, firm roles, project-level access, and Postgres RLS."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from factory import tenancy
from factory.api.main import app
from factory.config import settings
from factory.db import Base
from factory.models import (
    DomainTrack, Expert, ExpertStatus, Firm, FirmMembership, Project, Task, TaskType,
)


def _bearer(key, firm_id=None):
    h = {"Authorization": f"Bearer {key}"}
    if firm_id:
        h["X-Firm-Id"] = firm_id
    return h


@pytest.fixture()
def world(db):
    """Two firms, two projects in firm A, one in firm B, and A's owner/admin/member."""
    settings.auth_disabled = False
    with TestClient(app) as c:
        admin = _bearer(c.post("/bootstrap", json={
            "name": "Staff", "email": "staff@hdf.example.com"}).json()["api_key"])
        firm_a = c.post("/firms", json={"name": "A", "billing_email": "ap@a.example.com"},
                        headers=admin).json()["id"]
        firm_b = c.post("/firms", json={"name": "B", "billing_email": "ap@b.example.com"},
                        headers=admin).json()["id"]

        def project(firm, name):
            return c.post("/projects", json={
                "name": name, "firm_id": firm, "track": DomainTrack.FINANCIAL_ACCOUNTING.value,
                "task_type": TaskType.SFT.value}, headers=admin).json()["id"]

        pa1, pa2, pb = project(firm_a, "A1"), project(firm_a, "A2"), project(firm_b, "B1")
        owner = c.post("/users", json={"name": "Olive", "email": "olive@a.example.com",
                                       "role": "client", "firm_id": firm_a},
                       headers=admin).json()
        assert owner["firm_role"] == "owner"  # first user of a firm
        owner_h = _bearer(owner["api_key"])
        admin_user = c.post(f"/firms/{firm_a}/members", json={
            "name": "Ada", "email": "ada@a.example.com", "firm_role": "admin"},
            headers=owner_h).json()
        member = c.post(f"/firms/{firm_a}/members", json={
            "name": "Max", "email": "max@a.example.com"}, headers=owner_h).json()
        assert member["firm_role"] == "member"
        yield {
            "c": c, "db": db, "staff": admin, "firm_a": firm_a, "firm_b": firm_b,
            "pa1": pa1, "pa2": pa2, "pb": pb, "owner": owner_h,
            "admin": _bearer(admin_user["api_key"]), "admin_id": admin_user["user_id"],
            "member": _bearer(member["api_key"]), "member_id": member["user_id"],
            "owner_id": owner["user_id"],
        }
    settings.auth_disabled = True


def _project_ids(c, headers):
    r = c.get("/projects", headers=headers)
    assert r.status_code == 200
    return {p["id"] for p in r.json()}


def test_firm_admins_see_all_firm_projects_and_billing(world):
    c = world["c"]
    for who in ("owner", "admin"):
        assert _project_ids(c, world[who]) == {world["pa1"], world["pa2"]}
        assert c.get(f"/firms/{world['firm_a']}/billing", headers=world[who]).status_code == 200
        assert c.get(f"/firms/{world['firm_b']}/billing", headers=world[who]).status_code == 403
        assert c.get(f"/projects/{world['pb']}/tasks", headers=world[who]).status_code == 403


def test_member_sees_only_granted_projects_and_no_billing(world):
    c, m = world["c"], world["member"]
    assert _project_ids(c, m) == set()
    assert c.get(f"/projects/{world['pa1']}/tasks", headers=m).status_code == 403
    assert c.get(f"/firms/{world['firm_a']}/billing", headers=m).status_code == 403
    assert c.get(f"/firms/{world['firm_a']}/members", headers=m).status_code == 403

    r = c.post(f"/projects/{world['pa1']}/members", json={"user_id": world["member_id"]},
               headers=world["admin"])
    assert r.status_code == 201
    assert _project_ids(c, m) == {world["pa1"]}
    assert c.get(f"/projects/{world['pa1']}/tasks", headers=m).status_code == 200
    assert c.get(f"/projects/{world['pa2']}/tasks", headers=m).status_code == 403

    c.delete(f"/projects/{world['pa1']}/members/{world['member_id']}", headers=world["admin"])
    assert _project_ids(c, m) == set()


def test_cannot_grant_project_access_to_non_member(world):
    c = world["c"]
    outsider = c.post("/users", json={"name": "Bea", "email": "bea@b.example.com",
                                      "role": "client", "firm_id": world["firm_b"]},
                      headers=world["staff"]).json()["user_id"]
    r = c.post(f"/projects/{world['pa1']}/members", json={"user_id": outsider},
               headers=world["owner"])
    assert r.status_code == 404


def test_only_owners_manage_admins_and_last_owner_is_kept(world):
    c, fa = world["c"], world["firm_a"]
    r = c.post(f"/firms/{fa}/members", json={"name": "X", "email": "x@a.example.com",
                                             "firm_role": "owner"}, headers=world["admin"])
    assert r.status_code == 403
    r = c.patch(f"/firms/{fa}/members/{world['member_id']}", json={"firm_role": "admin"},
                headers=world["admin"])
    assert r.status_code == 403
    r = c.patch(f"/firms/{fa}/members/{world['member_id']}", json={"firm_role": "admin"},
                headers=world["owner"])
    assert r.status_code == 200 and r.json()["firm_role"] == "admin"
    r = c.patch(f"/firms/{fa}/members/{world['owner_id']}", json={"firm_role": "member"},
                headers=world["owner"])
    assert r.status_code == 409
    assert c.delete(f"/firms/{fa}/members/{world['owner_id']}",
                    headers=world["owner"]).status_code == 409


def test_removed_member_loses_access_and_key(world):
    c = world["c"]
    r = c.delete(f"/firms/{world['firm_a']}/members/{world['member_id']}",
                 headers=world["admin"])
    assert r.status_code == 200 and r.json()["deactivated"] is True
    assert c.get("/me", headers=world["member"]).status_code == 401


def test_multi_firm_user_switches_active_firm(world):
    c, db = world["c"], world["db"]
    db.add(FirmMembership(firm_id=world["firm_b"], user_id=world["admin_id"],
                          firm_role="admin"))
    db.commit()
    me = c.get("/me", headers=world["admin"]).json()
    assert me["firm_id"] == world["firm_a"]
    assert set(me["firm_ids"]) == {world["firm_a"], world["firm_b"]}
    assert _project_ids(c, _bearer_from(world["admin"], world["firm_b"])) == {world["pb"]}
    assert _project_ids(c, world["admin"]) == {world["pa1"], world["pa2"]}
    # a firm the user does not belong to
    assert c.get("/projects", headers=_bearer_from(world["member"], world["firm_b"])
                 ).status_code == 403


def _bearer_from(headers, firm_id):
    return {**headers, "X-Firm-Id": firm_id}


def test_clients_see_full_expert_identity(world):
    c, db = world["c"], world["db"]
    ex = Expert(name="Alice Chen", email="alice@experts.example.com", credentials=["CPA"],
                status=ExpertStatus.QUALIFIED.value)
    db.add(ex)
    db.flush()
    db.add(Task(project_id=world["pa1"], prompt="Explain ASC 842.", assigned_to=ex.id))
    db.commit()
    rows = c.get(f"/projects/{world['pa1']}/tasks", headers=world["owner"]).json()
    assert rows[0]["assigned_expert"] == {"id": ex.id, "name": "Alice Chen",
                                          "email": "alice@experts.example.com",
                                          "credentials": ["CPA"]}


def test_client_cannot_read_other_firms_task(world):
    c, db = world["c"], world["db"]
    t = Task(project_id=world["pb"], prompt="B's secret task")
    db.add(t)
    db.commit()
    assert c.get(f"/tasks/{t.id}", headers=world["owner"]).status_code == 403


# ------------------------------------------------------------------ real Postgres RLS

def _pg_url():
    url = os.environ.get("HDF_TEST_POSTGRES_URL") or os.environ.get("DATABASE_URL_UNPOOLED")
    if not url:
        return None
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


@pytest.fixture()
def pg():
    url = _pg_url()
    if not url:
        pytest.skip("no Postgres URL (set HDF_TEST_POSTGRES_URL) — RLS test skipped")
    schema = f"hdf_rls_test_{uuid.uuid4().hex[:10]}"
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_conn, _):
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
        dbapi_conn.commit()

    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            tenancy.install_rls(conn)
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


def test_postgres_rls_hides_other_firms_rows(pg):
    with pg() as s:
        a = Firm(name="A", billing_email="ap@a.example.com")
        b = Firm(name="B", billing_email="ap@b.example.com")
        s.add_all([a, b])
        s.flush()
        pa = Project(name="PA", firm_id=a.id, track="financial_accounting", task_type="sft",
                     rubric_id="sft_default", created_by="t")
        pb = Project(name="PB", firm_id=b.id, track="financial_accounting", task_type="sft",
                     rubric_id="sft_default", created_by="t")
        s.add_all([pa, pb])
        s.flush()
        s.add_all([Task(project_id=pa.id, prompt="a"), Task(project_id=pb.id, prompt="b")])
        s.commit()
        a_id, b_id, pa_id, pb_id = a.id, b.id, pa.id, pb.id

    with pg() as s:
        tenancy.enter_tenant(s, a_id)
        # deliberately unfiltered queries: the database, not the app, enforces isolation
        assert s.execute(select(Firm.id)).scalars().all() == [a_id]
        assert s.execute(select(Project.id)).scalars().all() == [pa_id]
        assert [t.project_id for t in s.execute(select(Task)).scalars()] == [pa_id]
        assert s.get(Project, pb_id) is None
        s.commit()
        # scope survives commit: a new transaction re-applies the tenant
        assert s.execute(select(Project.id)).scalars().all() == [pa_id]
        assert s.execute(text("SELECT current_user")).scalar() == tenancy.TENANT_ROLE

    with pg() as s:
        tenancy.enter_tenant(s, a_id)
        s.add(Project(name="sneaky", firm_id=b_id, track="financial_accounting",
                      task_type="sft", rubric_id="sft_default", created_by="t"))
        with pytest.raises(Exception, match="row-level security"):
            s.flush()

    with pg() as s:  # privileged connection role is unaffected
        assert len(s.execute(select(Project.id)).scalars().all()) == 2
