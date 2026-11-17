"""Publication idempotency and session security metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0005_security"
down_revision = "0004_jobs"
branch_labels = None
depends_on = None


def upgrade():
    # Existing sessions are revoked: no trustworthy credential fingerprint exists.
    op.execute("DELETE FROM auth_sessions")
    with op.batch_alter_table("auth_sessions") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("password_fingerprint", sa.String(64), nullable=False, server_default=""))
    op.execute("""
        UPDATE metric_events SET publication_id = (
            SELECT MIN(keep.id) FROM publications AS keep
            JOIN publications AS duplicate ON keep.clip_id = duplicate.clip_id
                AND keep.platform = duplicate.platform
            WHERE duplicate.id = metric_events.publication_id
        ) WHERE publication_id IN (SELECT id FROM publications)
    """)
    op.execute("DELETE FROM publications WHERE id NOT IN (SELECT MIN(id) FROM publications GROUP BY clip_id, platform)")
    with op.batch_alter_table("publications") as batch:
        batch.create_unique_constraint("uq_publication_clip_platform", ["clip_id", "platform"])


def downgrade():
    with op.batch_alter_table("publications") as batch:
        batch.drop_constraint("uq_publication_clip_platform", type_="unique")
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_column("password_fingerprint")
        batch.drop_column("last_seen_at")
