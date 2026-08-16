"""add docs table

Revision ID: 7c8d9e0f1a2
Revises: 6b7c8d9e0f1a
Create Date: 2026-03-16 20:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "7c8d9e0f1a2"
down_revision = "6b7c8d9e0f1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_docs_title"), "docs", ["title"], unique=False)
    op.create_index(op.f("ix_docs_is_visible"), "docs", ["is_visible"], unique=False)
    op.alter_column("docs", "is_visible", server_default=None)
    op.alter_column("docs", "sort_order", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_docs_is_visible"), table_name="docs")
    op.drop_index(op.f("ix_docs_title"), table_name="docs")
    op.drop_table("docs")
