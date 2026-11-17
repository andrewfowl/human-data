"""audit_events.prev_hash unique — a forked chain fails loudly

Revision ID: a1c0de7f0001
Revises: 4bafa6e29c73
Create Date: 2026-11-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1c0de7f0001"
down_revision: Union[str, Sequence[str], None] = "4bafa6e29c73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.create_unique_constraint("uq_audit_events_prev_hash", ["prev_hash"])


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_constraint("uq_audit_events_prev_hash", type_="unique")
