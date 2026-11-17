"""Align migrated schemas with application foreign keys and nullability."""
from alembic import op

revision = "0007_schema_hardening"
down_revision = "0006_publication_clip_fk"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("UPDATE sources SET metadata_json='{}' WHERE metadata_json IS NULL")
        op.execute("UPDATE sources SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE clips SET evidence='{}' WHERE evidence IS NULL")
        op.execute("UPDATE clips SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE rights_records SET allowed_platforms='[]' WHERE allowed_platforms IS NULL")
        op.execute("UPDATE rights_records SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE workflow_runs SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE workflow_runs SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")
        op.execute("UPDATE ledger_entries SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE metric_events SET observed_at=CURRENT_TIMESTAMP WHERE observed_at IS NULL")
        op.execute("UPDATE publications SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        with op.batch_alter_table("rights_records") as batch:
            batch.alter_column("allowed_platforms", nullable=False)
            batch.alter_column("created_at", nullable=False)
            batch.create_foreign_key("fk_rights_records_source_id_sources", "sources", ["source_id"], ["id"])
        with op.batch_alter_table("clips") as batch:
            batch.alter_column("evidence", nullable=False)
            batch.alter_column("created_at", nullable=False)
            batch.create_foreign_key("fk_clips_source_id_sources", "sources", ["source_id"], ["id"])
        with op.batch_alter_table("workflow_runs") as batch:
            batch.alter_column("created_at", nullable=False)
            batch.alter_column("updated_at", nullable=False)
            batch.create_foreign_key("fk_workflow_runs_source_id_sources", "sources", ["source_id"], ["id"])
        with op.batch_alter_table("sources") as batch:
            batch.alter_column("metadata_json", nullable=False)
            batch.alter_column("created_at", nullable=False)
        with op.batch_alter_table("ledger_entries") as batch:
            batch.alter_column("created_at", nullable=False)
        with op.batch_alter_table("metric_events") as batch:
            batch.alter_column("observed_at", nullable=False)
        with op.batch_alter_table("publications") as batch:
            batch.alter_column("created_at", nullable=False)
    else:
        op.execute("UPDATE sources SET metadata_json='{}'::json WHERE metadata_json IS NULL")
        op.execute("UPDATE sources SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE clips SET evidence='{}'::json WHERE evidence IS NULL")
        op.execute("UPDATE clips SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE rights_records SET allowed_platforms='[]'::json WHERE allowed_platforms IS NULL")
        op.execute("UPDATE rights_records SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE workflow_runs SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE workflow_runs SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")
        op.execute("UPDATE ledger_entries SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.execute("UPDATE metric_events SET observed_at=CURRENT_TIMESTAMP WHERE observed_at IS NULL")
        op.execute("UPDATE publications SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        op.create_foreign_key("fk_rights_records_source_id_sources", "rights_records", "sources", ["source_id"], ["id"])
        op.create_foreign_key("fk_clips_source_id_sources", "clips", "sources", ["source_id"], ["id"])
        op.create_foreign_key("fk_workflow_runs_source_id_sources", "workflow_runs", "sources", ["source_id"], ["id"])
        for table, column in (
            ("sources", "metadata_json"), ("sources", "created_at"),
            ("clips", "evidence"), ("clips", "created_at"),
            ("rights_records", "allowed_platforms"), ("rights_records", "created_at"),
            ("workflow_runs", "created_at"), ("workflow_runs", "updated_at"),
            ("ledger_entries", "created_at"), ("metric_events", "observed_at"),
            ("publications", "created_at"),
        ):
            op.alter_column(table, column, nullable=False)
