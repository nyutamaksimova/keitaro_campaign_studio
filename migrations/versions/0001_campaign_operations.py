"""Create durable campaign operation journal."""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'campaign_operations',
        sa.Column('request_id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('geo', sa.String(2), nullable=False),
        sa.Column('offer_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('alias', sa.String(64), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('campaign_url', sa.Text(), nullable=True),
        sa.Column('stream_ids', sa.JSON(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table('campaign_operations')
