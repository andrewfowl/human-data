"""Database-enforced tenant isolation (Postgres row-level security).

The application's connection role (e.g. Neon's `neondb_owner`) has BYPASSRLS,
so policies alone would never apply to it. Instead, every transaction made on
behalf of a client user switches to the dedicated `hdf_tenant` role
(NOLOGIN, NOBYPASSRLS) with `SET LOCAL ROLE` and pins the active firm in the
`app.firm_id` setting. Policies scoped `TO hdf_tenant` then hide every row
belonging to another firm — even if an application query forgets its filter.

Both settings are transaction-local, so they are re-applied at the start of
each transaction (`after_begin`) and vanish at commit/rollback; a pooled
connection never leaks a tenant into the next request. An unset firm matches
no rows (fail closed). Staff, experts, workers and migrations run as the
connection role and rely on application-level authorization.

Tables without tenant data (users, api_keys, experts, qualifications,
audit_events, jobs, idempotency_records) are not row-restricted.
"""

from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

TENANT_ROLE = "hdf_tenant"
_TENANT = "NULLIF(current_setting('app.firm_id', true), '')"
_INFO_KEY = "tenant_firm_id"

# table -> USING/WITH CHECK predicate evaluated as hdf_tenant
POLICIES: dict[str, str] = {
    "firms": f"id = {_TENANT}",
    "projects": f"firm_id = {_TENANT}",
    "usage_events": f"firm_id = {_TENANT}",
    "invoices": f"firm_id = {_TENANT}",
    "firm_memberships": f"firm_id = {_TENANT}",
    "project_memberships": f"firm_id = {_TENANT}",
    "rate_cards": f"firm_id IS NULL OR firm_id = {_TENANT}",
    # Child tables inherit isolation through the (already restricted) parent.
    "tasks": "project_id IN (SELECT id FROM projects)",
    "export_batches": "project_id IN (SELECT id FROM projects)",
    "submissions": "task_id IN (SELECT id FROM tasks)",
    "reviews": "submission_id IN (SELECT id FROM submissions)",
}


def install_rls(conn: Connection) -> None:
    """Idempotently create the tenant role, grants and policies in the current schema."""
    if conn.dialect.name != "postgresql":
        return
    conn.execute(text(f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TENANT_ROLE}') THEN
            CREATE ROLE {TENANT_ROLE} NOLOGIN NOBYPASSRLS;
          END IF;
        END $$"""))
    conn.execute(text(f"GRANT {TENANT_ROLE} TO CURRENT_USER"))
    conn.execute(text(f"""
        DO $$ BEGIN
          EXECUTE format('GRANT USAGE ON SCHEMA %I TO {TENANT_ROLE}', current_schema());
          EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I
                          TO {TENANT_ROLE}', current_schema());
          EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I
                          TO {TENANT_ROLE}', current_schema());
        END $$"""))
    for table, predicate in POLICIES.items():
        policy = f"{table}_tenant_isolation"
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        conn.execute(text(
            f"CREATE POLICY {policy} ON {table} FOR ALL TO {TENANT_ROLE} "
            f"USING ({predicate}) WITH CHECK ({predicate})"))


def uninstall_rls(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    for table in POLICIES:
        conn.execute(text(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}"))
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))


def _apply(conn: Connection, firm_id: str) -> None:
    conn.execute(text(f"SET LOCAL ROLE {TENANT_ROLE}"))
    conn.execute(text("SELECT set_config('app.firm_id', :f, true)"), {"f": firm_id})


def enter_tenant(db: Session, firm_id: str) -> None:
    """Confine this session to one firm for the rest of its life."""
    db.info[_INFO_KEY] = firm_id
    if db.get_bind().dialect.name == "postgresql":
        db.flush()  # pending privileged writes (e.g. api_key.last_used_at) stay privileged
        _apply(db.connection(), firm_id)


@event.listens_for(Session, "after_begin")
def _reapply_tenant(session: Session, transaction, connection: Connection) -> None:
    firm_id = session.info.get(_INFO_KEY)
    if firm_id and connection.dialect.name == "postgresql":
        _apply(connection, firm_id)
