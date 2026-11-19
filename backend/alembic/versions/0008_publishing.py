"""Add connected accounts, OAuth state, and durable publication jobs."""
import sqlalchemy as sa
from alembic import op

revision = "0008_publishing"
down_revision = "0007_schema_hardening"
branch_labels = None
depends_on = None


def timestamp(name, nullable=True):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    op.create_table(
        "connected_accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(500), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column("access_token", sa.Text),
        sa.Column("refresh_token", sa.Text),
        timestamp("expires_at"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("daily_upload_limit", sa.Integer, nullable=False),
        timestamp("created_at", False),
        timestamp("updated_at", False),
        sa.UniqueConstraint("platform", "external_account_id", name="uq_account_platform_external"),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(64), primary_key=True),
        sa.Column("session_binding", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("code_verifier", sa.String(128)),
        timestamp("created_at", False),
        timestamp("expires_at", False),
        timestamp("consumed_at"),
    )
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])
    with op.batch_alter_table("publications") as batch:
        batch.drop_constraint("uq_publication_clip_platform", type_="unique")
        batch.add_column(sa.Column("account_id", sa.Integer))
        batch.create_foreign_key("fk_publications_account", "connected_accounts", ["account_id"], ["id"])
        batch.create_index("ix_publications_account_id", ["account_id"])
        batch.add_column(sa.Column("idempotency_key", sa.String(64)))
        batch.create_unique_constraint("uq_publications_idempotency_key", ["idempotency_key"])
        batch.add_column(sa.Column("media_sha256", sa.String(64)))
        batch.add_column(sa.Column("title", sa.String(100), server_default="", nullable=False))
        batch.add_column(sa.Column("description", sa.Text, server_default="", nullable=False))
        batch.add_column(sa.Column("privacy_status", sa.String(32), server_default="private", nullable=False))
        batch.add_column(sa.Column("dry_run", sa.Boolean, server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column("attempts", sa.Integer, server_default="0", nullable=False))
        batch.add_column(timestamp("next_attempt_at"))
        batch.add_column(sa.Column("bytes_sent", sa.BigInteger, server_default="0", nullable=False))
        batch.add_column(sa.Column("bytes_total", sa.BigInteger, server_default="0", nullable=False))
        batch.add_column(sa.Column("upload_checkpoint", sa.Text))
        batch.add_column(timestamp("upload_started_at"))
        batch.add_column(sa.Column("external_url", sa.String(1000)))
        batch.add_column(sa.Column("error_class", sa.String(32)))
        batch.add_column(sa.Column("last_error", sa.Text))
        batch.add_column(timestamp("published_at"))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.execute("UPDATE publications SET updated_at = created_at")
    op.create_table(
        "publication_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("publication_id", sa.Integer, sa.ForeignKey("publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("error_class", sa.String(32)),
        timestamp("created_at", False),
    )
    op.create_index("ix_publication_events_publication_id", "publication_events", ["publication_id"])
    op.execute("""
        INSERT INTO publication_events (publication_id, from_status, to_status, actor, error_class, created_at)
        SELECT id, status, 'failed', 'migration', 'legacy', CURRENT_TIMESTAMP FROM publications WHERE status = 'queued'
    """)
    op.execute("""
        UPDATE publications SET status = 'failed', error_class = 'legacy',
        last_error = 'Legacy publication has no connected account or upload job', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'queued'
    """)
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("uq_jobs_active_source")
        predicate = sa.text("kind = 'process_source' AND status IN ('queued', 'running')")
        batch.create_index("uq_jobs_active_source", ["source_id"], unique=True, sqlite_where=predicate, postgresql_where=predicate)
        batch.alter_column("workflow_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("publication_id", sa.Integer))
        batch.create_foreign_key("fk_jobs_publication", "publications", ["publication_id"], ["id"], ondelete="CASCADE")
        batch.create_unique_constraint("uq_jobs_publication_id", ["publication_id"])


def downgrade():
    raise RuntimeError("Restore a pre-0008 database backup to downgrade publication data")
