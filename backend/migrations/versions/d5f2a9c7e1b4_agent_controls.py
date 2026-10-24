"""agent_controls + control_events: platform kill switch and its history (w4-3)

Revision ID: d5f2a9c7e1b4
Revises: c4e8f1a2b3d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mssql

revision: str = "d5f2a9c7e1b4"
down_revision: Union[str, None] = "c4e8f1a2b3d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts():
    # microsecond timestamps on SQL Server, same as c4e8f1a2b3d5
    return mssql.DATETIME2(precision=6) if op.get_bind().dialect.name == "mssql" else sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "agent_controls",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column("changed_by", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
        sa.Column("changed_at", _ts(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", _ts(), nullable=False),
        sa.Column("actor", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column("action", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("control_events")
    op.drop_table("agent_controls")
