"""add canceled invitation status

Revision ID: 4f7b8d2c1a9e
Revises: 9c2e6f4a1b7d
Create Date: 2026-03-24 12:35:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "4f7b8d2c1a9e"
down_revision = "9c2e6f4a1b7d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            ALTER TYPE invitationstatus ADD VALUE 'CANCELED';
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END
        $$;
        """
    )


def downgrade() -> None:
    pass
