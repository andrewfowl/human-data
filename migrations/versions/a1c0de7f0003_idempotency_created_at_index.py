"""index idempotency_records.created_at for TTL purge

Revision ID: a1c0de7f0003
Revises: a1c0de7f0002
Create Date: 2026-11-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1c0de7f0003"
down_revision: Union[str, Sequence[str], None] = "a1c0de7f0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_idempotency_records_created_at", "idempotency_records",
                    ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
