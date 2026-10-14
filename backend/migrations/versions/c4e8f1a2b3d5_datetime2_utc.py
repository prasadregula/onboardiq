"""timestamps: DATETIME -> DATETIME2(6) on SQL Server (microsecond precision for the audit hash chain)

Revision ID: c4e8f1a2b3d5
Revises: b7c1e4d2a9f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

revision: str = "c4e8f1a2b3d5"
down_revision: Union[str, None] = "b7c1e4d2a9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, nullable)
COLUMNS = [
    ("cases", "created_at", False), ("cases", "updated_at", False),
    ("audit_events", "ts", False),
    ("case_runs", "started_at", False), ("case_runs", "finished_at", True),
    ("documents", "extracted_at", True), ("documents", "created_at", False),
    ("review_tasks", "due_at", True), ("review_tasks", "created_at", False), ("review_tasks", "resolved_at", True),
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "mssql":
        return  # SQLite/Postgres: DateTime already keeps microseconds
    for table, col, nullable in COLUMNS:
        op.alter_column(table, col, existing_type=sa.DateTime(), type_=mssql.DATETIME2(precision=6),
                        existing_nullable=nullable)


def downgrade() -> None:
    if op.get_bind().dialect.name != "mssql":
        return
    for table, col, nullable in COLUMNS:
        op.alter_column(table, col, existing_type=mssql.DATETIME2(precision=6), type_=sa.DateTime(),
                        existing_nullable=nullable)
