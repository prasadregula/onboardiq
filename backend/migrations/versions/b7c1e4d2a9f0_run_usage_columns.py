"""case_runs: model usage columns (llm_calls, tokens_cached, llm_latency_ms, cost_usd)

Revision ID: b7c1e4d2a9f0
Revises: aa332da248bd
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c1e4d2a9f0"
down_revision = "aa332da248bd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("case_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tokens_cached", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("llm_latency_ms", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("case_runs", schema=None) as batch_op:
        batch_op.drop_column("cost_usd")
        batch_op.drop_column("llm_latency_ms")
        batch_op.drop_column("llm_calls")
        batch_op.drop_column("tokens_cached")
