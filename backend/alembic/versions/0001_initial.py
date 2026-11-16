"""initial Soe schema"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("sources", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("platform", sa.String(32), nullable=False), sa.Column("external_id", sa.String(255), nullable=False), sa.Column("title", sa.String(500), nullable=False), sa.Column("url", sa.String(1000), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("media_path", sa.String(2000)), sa.Column("media_sha256", sa.String(64)), sa.Column("metadata_json", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_sources_external_id", "sources", ["external_id"], unique=True)
    op.create_table("rights_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_id", sa.Integer(), nullable=False), sa.Column("authorization_type", sa.String(64), nullable=False), sa.Column("commercial_use", sa.Boolean(), nullable=False), sa.Column("publishing_allowed", sa.Boolean(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("evidence", sa.Text(), nullable=False), sa.Column("allowed_platforms", sa.JSON()), sa.Column("revoked", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_rights_records_source_id", "rights_records", ["source_id"])
    op.create_table("clips", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_id", sa.Integer(), nullable=False), sa.Column("start_seconds", sa.Float(), nullable=False), sa.Column("end_seconds", sa.Float(), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("hook", sa.String(500), nullable=False), sa.Column("score", sa.Float(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("evidence", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_clips_source_id", "clips", ["source_id"])
    op.create_table("workflow_runs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_id", sa.Integer()), sa.Column("state", sa.String(64), nullable=False), sa.Column("cost_cents", sa.Integer(), nullable=False), sa.Column("error", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.create_table("publications", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("clip_id", sa.Integer(), nullable=False), sa.Column("platform", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("external_id", sa.String(255)), sa.Column("error", sa.Text()), sa.Column("scheduled_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_publications_clip_id", "publications", ["clip_id"])
    op.create_table("metric_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("publication_id", sa.Integer(), nullable=False), sa.Column("metric", sa.String(64), nullable=False), sa.Column("value", sa.Float(), nullable=False), sa.Column("verified", sa.Boolean(), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_metric_events_publication_id", "metric_events", ["publication_id"])
    op.create_table("ledger_entries", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("kind", sa.String(16), nullable=False), sa.Column("category", sa.String(64), nullable=False), sa.Column("cents", sa.Integer(), nullable=False), sa.Column("verified", sa.Boolean(), nullable=False), sa.Column("note", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for table in ("ledger_entries", "metric_events", "publications", "workflow_runs", "clips", "rights_records", "sources"):
        op.drop_table(table)
