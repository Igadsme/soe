"""Require publications to reference existing clips."""
from alembic import op

revision = "0006_publication_clip_fk"
down_revision = "0005_security"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    op.execute("DELETE FROM publications WHERE NOT EXISTS (SELECT 1 FROM clips WHERE clips.id = publications.clip_id)")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("publications") as batch:
            batch.create_foreign_key("fk_publications_clip_id_clips", "clips", ["clip_id"], ["id"], ondelete="CASCADE")
    else:
        op.create_foreign_key("fk_publications_clip_id_clips", "publications", "clips", ["clip_id"], ["id"], ondelete="CASCADE")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("publications") as batch:
            batch.drop_constraint("fk_publications_clip_id_clips", type_="foreignkey")
    else:
        op.drop_constraint("fk_publications_clip_id_clips", "publications", type_="foreignkey")
