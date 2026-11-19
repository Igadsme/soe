"""Persist orchestration policy, schedules, audit and job controls."""
import sqlalchemy as sa
from alembic import op

revision = "0009_automation"
down_revision = "0008_publishing"
branch_labels = None
depends_on = None


def timestamp(name, nullable=True):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    policy = op.create_table(
        "automation_policy",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("configuration", sa.JSON, nullable=False),
        sa.Column("emergency_stopped", sa.Boolean, nullable=False),
        timestamp("worker_heartbeat_at"),
    )
    op.bulk_insert(policy, [{"id": 1, "configuration": {}, "emergency_stopped": False}])
    op.create_table(
        "discovery_schedules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("channel_allowlist", sa.JSON, nullable=False),
        sa.Column("interval_minutes", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("max_results", sa.Integer, nullable=False),
        timestamp("last_run_at"),
        timestamp("next_run_at", False),
        sa.Column("last_status", sa.String(64)),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        timestamp("created_at", False),
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["entity"])
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("media_revision", sa.Integer, server_default="0", nullable=False))
    with op.batch_alter_table("publications") as batch:
        batch.add_column(sa.Column("automated", sa.Boolean, server_default=sa.false(), nullable=False))
    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("source_id", existing_type=sa.Integer, nullable=True)
        batch.drop_constraint("uq_jobs_publication_id", type_="unique")
        batch.drop_index("uq_jobs_active_source")
        active = "status IN ('queued', 'running', 'cancel_requested')"
        for kind, column in (("process_source", "source_id"), ("publish", "publication_id")):
            predicate = sa.text(f"kind = '{kind}' AND {active}")
            name = "source" if column == "source_id" else "publication"
            batch.create_index(f"uq_jobs_active_{name}", [column], unique=True,
                               sqlite_where=predicate, postgresql_where=predicate)
        batch.add_column(sa.Column("replay_of_id", sa.String(64)))
        batch.create_foreign_key("fk_jobs_replay_of", "jobs", ["replay_of_id"], ["id"])
        batch.create_unique_constraint("uq_jobs_replay_of", ["replay_of_id"])
        batch.add_column(sa.Column("payload", sa.JSON))
        batch.add_column(timestamp("deadline_at"))
        batch.add_column(timestamp("expires_at"))
        for name in ("error_class", "wait_reason", "interruption"):
            batch.add_column(sa.Column(name, sa.String(32)))


def downgrade():
    raise RuntimeError("Restore a pre-0009 database backup to downgrade automation data")
