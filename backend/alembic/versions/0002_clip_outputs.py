"""persist rendered clip and subtitle paths"""
from alembic import op
import sqlalchemy as sa

revision = "0002_clip_outputs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("output_path", sa.String(2000)))
    op.add_column("clips", sa.Column("subtitle_path", sa.String(2000)))


def downgrade() -> None:
    op.drop_column("clips", "subtitle_path")
    op.drop_column("clips", "output_path")
