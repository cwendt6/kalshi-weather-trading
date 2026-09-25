"""Add ML training columns to ForecastDB and create CalibrationSummaryDB

Adds feature storage, resolution tracking, and calibration fields to support:
- Point 2: Correct ML framing (y ∈ {0,1} resolution outcome)
- Point 3: Feature engineering (features_json, meta-features)
- Point 7: Force calibration (calibration_bucket, brier_score)
- Point 11: Don't-trade filter (spread, liquidity, market age metadata)

Revision ID: ml_training_001
Revises: 374cd1bb0593
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = 'ml_training_001'
down_revision = '374cd1bb0593'
branch_labels = None
depends_on = None


def upgrade():
    # === Add new columns to forecasts table ===

    # ML Training Data (Point 3: feature engineering)
    op.add_column('forecasts', sa.Column('features_json', sa.Text(), nullable=True))
    op.add_column('forecasts', sa.Column('domain', sa.String(30), nullable=True))

    # Resolution Tracking (Point 2: correct framing — y ∈ {0,1})
    op.add_column('forecasts', sa.Column('resolution', sa.Integer(), nullable=True))
    op.add_column('forecasts', sa.Column('resolved_at', sa.DateTime(), nullable=True))

    # Market Context Meta-Features (Point 3: underrated features)
    op.add_column('forecasts', sa.Column('spread_cents', sa.Integer(), nullable=True))
    op.add_column('forecasts', sa.Column('volume_at_forecast', sa.Integer(), nullable=True))
    op.add_column('forecasts', sa.Column('open_interest_at_forecast', sa.Integer(), nullable=True))
    op.add_column('forecasts', sa.Column('hours_to_expiry', sa.Float(), nullable=True))
    op.add_column('forecasts', sa.Column('market_age_hours', sa.Float(), nullable=True))

    # Calibration (Point 7: force calibration)
    op.add_column('forecasts', sa.Column('calibration_bucket', sa.Float(), nullable=True))
    op.add_column('forecasts', sa.Column('brier_score', sa.Float(), nullable=True))

    # Indexes for efficient querying
    op.create_index('idx_forecasts_domain', 'forecasts', ['domain'])
    op.create_index('idx_forecasts_resolution', 'forecasts', ['resolution'])
    op.create_index('idx_forecasts_calibration', 'forecasts', ['calibration_bucket', 'resolution'])

    # === Create calibration_summaries table ===
    op.create_table(
        'calibration_summaries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('computed_at', sa.DateTime(), nullable=False, index=True),
        sa.Column('method', sa.String(50), nullable=False, index=True),
        sa.Column('domain', sa.String(30), nullable=True),
        sa.Column('bucket', sa.Float(), nullable=False),
        sa.Column('bucket_count', sa.Integer(), nullable=False),
        sa.Column('actual_resolution_rate', sa.Float(), nullable=True),
        sa.Column('predicted_mean', sa.Float(), nullable=True),
        sa.Column('brier_score', sa.Float(), nullable=True),
        sa.Column('total_forecasts', sa.Integer(), nullable=True),
        sa.Column('total_resolved', sa.Integer(), nullable=True),
        sa.Column('overall_brier_score', sa.Float(), nullable=True),
        sa.Column('overall_log_loss', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('idx_calibration_method_bucket', 'calibration_summaries', ['method', 'bucket'])
    op.create_index('idx_calibration_computed', 'calibration_summaries', ['computed_at'])


def downgrade():
    # Drop calibration table
    op.drop_table('calibration_summaries')

    # Remove forecast columns
    op.drop_index('idx_forecasts_calibration', 'forecasts')
    op.drop_index('idx_forecasts_resolution', 'forecasts')
    op.drop_index('idx_forecasts_domain', 'forecasts')

    op.drop_column('forecasts', 'brier_score')
    op.drop_column('forecasts', 'calibration_bucket')
    op.drop_column('forecasts', 'market_age_hours')
    op.drop_column('forecasts', 'hours_to_expiry')
    op.drop_column('forecasts', 'open_interest_at_forecast')
    op.drop_column('forecasts', 'volume_at_forecast')
    op.drop_column('forecasts', 'spread_cents')
    op.drop_column('forecasts', 'resolved_at')
    op.drop_column('forecasts', 'resolution')
    op.drop_column('forecasts', 'domain')
    op.drop_column('forecasts', 'features_json')
