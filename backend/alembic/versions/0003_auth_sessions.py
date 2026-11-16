"""Server-side session revocation."""
from alembic import op
import sqlalchemy as sa
revision = "0003_auth_sessions"
down_revision = "0002_clip_outputs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("auth_sessions", sa.Column("id", sa.String(64), primary_key=True), sa.Column("username", sa.String(255), nullable=False), sa.Column("expires_at", sa.Integer(), nullable=False), sa.Column("revoked_at", sa.Integer(), nullable=True))
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])


def downgrade():
    op.drop_table("auth_sessions")
