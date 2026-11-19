"""Revenue/metric provenance, analytics collection attempts, revenue imports."""
import sqlalchemy as sa
from alembic import op

revision = "0010_provenance"
down_revision = "0009_automation"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("metric_events") as batch:
        batch.add_column(sa.Column("source", sa.String(32), nullable=False, server_default="api"))
        batch.add_column(sa.Column("provenance", sa.String(16), nullable=False, server_default="verified"))
        batch.add_column(sa.Column("window", sa.String(16), nullable=True))
    op.execute("UPDATE metric_events SET provenance = 'manual' WHERE verified = false")
    with op.batch_alter_table("ledger_entries") as batch:
        batch.add_column(sa.Column("provenance", sa.String(24), nullable=False, server_default="manual"))
        batch.add_column(sa.Column("currency", sa.String(3), nullable=False, server_default="USD"))
        batch.add_column(sa.Column("platform", sa.String(16), nullable=True))
        batch.add_column(sa.Column("period_start", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("period_end", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("publication_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("source_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("reference", sa.Text, nullable=True))
        batch.add_column(sa.Column("import_id", sa.Integer, nullable=True))
        batch.create_index("ix_ledger_entries_import_id", ["import_id"])
    # Unverified expenses were processing estimates; verified rows came from an external record.
    op.execute("UPDATE ledger_entries SET provenance = 'estimated' WHERE kind = 'expense' AND verified = false")
    op.execute("UPDATE ledger_entries SET provenance = 'verified_platform' WHERE kind = 'revenue' AND verified = true")
    op.create_table(
        "analytics_collections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("publication_id", sa.Integer, nullable=False, index=True),
        sa.Column("window", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("publication_id", "window", name="uq_analytics_collection_window"),
    )
    op.create_table(
        "revenue_imports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("rows", sa.Integer, nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("revenue_imports")
    op.drop_table("analytics_collections")
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_index("ix_ledger_entries_import_id")
        for name in ("import_id", "reference", "source_id", "publication_id", "period_end", "period_start", "platform", "currency", "provenance"):
            batch.drop_column(name)
    with op.batch_alter_table("metric_events") as batch:
        for name in ("window", "provenance", "source"):
            batch.drop_column(name)
