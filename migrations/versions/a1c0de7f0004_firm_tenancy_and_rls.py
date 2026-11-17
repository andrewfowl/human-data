"""firm memberships, project memberships, and Postgres row-level security

Existing client users keep the full-firm access they had before: the earliest
client user of each firm becomes its owner, the rest become admins.

Revision ID: a1c0de7f0004
Revises: a1c0de7f0003
Create Date: 2026-11-17
"""
import sys
import os
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from factory import tenancy  # noqa: E402

revision: str = "a1c0de7f0004"
down_revision: Union[str, Sequence[str], None] = "a1c0de7f0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "firm_memberships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("firm_role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("firm_id", "user_id", name="uq_firm_membership"),
    )
    op.create_index("ix_firm_memberships_firm_id", "firm_memberships", ["firm_id"])
    op.create_index("ix_firm_memberships_user_id", "firm_memberships", ["user_id"])
    op.create_table(
        "project_memberships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_membership"),
    )
    op.create_index("ix_project_memberships_firm_id", "project_memberships", ["firm_id"])
    op.create_index("ix_project_memberships_project_id", "project_memberships", ["project_id"])
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"])

    conn = op.get_bind()
    clients = conn.execute(sa.text(
        "SELECT id, firm_id, created_at FROM users "
        "WHERE role = 'client' AND firm_id IS NOT NULL ORDER BY firm_id, created_at, id"
    )).all()
    seen: set[str] = set()
    for user_id, firm_id, created_at in clients:
        role = "admin" if firm_id in seen else "owner"
        seen.add(firm_id)
        conn.execute(sa.text(
            "INSERT INTO firm_memberships (id, firm_id, user_id, firm_role, created_at) "
            "VALUES (:id, :f, :u, :r, :c)"),
            {"id": uuid4().hex, "f": firm_id, "u": user_id, "r": role, "c": created_at})

    tenancy.install_rls(conn)


def downgrade() -> None:
    tenancy.uninstall_rls(op.get_bind())
    op.drop_table("project_memberships")
    op.drop_table("firm_memberships")
