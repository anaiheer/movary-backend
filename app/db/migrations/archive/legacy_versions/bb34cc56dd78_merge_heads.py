"""merge heads

Revision ID: bb34cc56dd78
Revises: 1a2b3c4d5e6f, aa12bb34cc56
Create Date: 2026-01-23 00:00:00.000000
"""

# This is a merge migration.

revision = "bb34cc56dd78"
down_revision = ("1a2b3c4d5e6f", "aa12bb34cc56")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
