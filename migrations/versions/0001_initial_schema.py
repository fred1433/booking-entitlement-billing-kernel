"""The schema, in one migration.

Several of the guarantees in this sample are unique indexes and check
constraints rather than service code, and they are created here. A test that
tries to break each of them in raw SQL lives in
``tests/test_database_guarantees.py``.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('billing_runs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_billing_runs_period'), 'billing_runs', ['period'], unique=False)
    op.create_table('bookings',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('partner', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('customer_ref', sa.String(length=128), nullable=False),
    sa.Column('product_code', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('amount_basis', sa.String(length=16), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('updated_at_raw', sa.String(length=64), nullable=False),
    sa.Column('updated_at_lo', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at_hi', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at_ambiguous', sa.Boolean(), nullable=False),
    sa.Column('payload_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('undecided_reason', sa.Text(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_applied_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_delivery_id', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('partner', 'external_id', name='uq_booking_identity')
    )
    op.create_table('import_batches',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('partner', sa.String(length=64), nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('filename', sa.String(length=256), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('applied_rows', sa.Integer(), nullable=False),
    sa.Column('quarantined_rows', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('batch_id')
    )
    op.create_index(op.f('ix_import_batches_partner'), 'import_batches', ['partner'], unique=False)
    op.create_table('journal_entries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('subject', sa.String(length=160), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_journal_entries_action'), 'journal_entries', ['action'], unique=False)
    op.create_index(op.f('ix_journal_entries_outcome'), 'journal_entries', ['outcome'], unique=False)
    op.create_table('processed_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('partner', sa.String(length=64), nullable=False),
    sa.Column('source_event_id', sa.String(length=128), nullable=False),
    sa.Column('first_delivery_id', sa.BigInteger(), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('partner', 'source_event_id', name='uq_processed_event')
    )
    op.create_table('quarantine_items',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('partner', sa.String(length=64), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('delivery_id', sa.BigInteger(), nullable=True),
    sa.Column('kind', sa.String(length=48), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('needed_from_partner', sa.Text(), nullable=False),
    sa.Column('raised_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_quarantine_items_external_id'), 'quarantine_items', ['external_id'], unique=False)
    op.create_index(op.f('ix_quarantine_items_partner'), 'quarantine_items', ['partner'], unique=False)
    op.create_table('simulated_provider_charges',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('charge_ref', sa.String(length=128), nullable=False),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key', name='uq_simulated_charge_key')
    )
    op.create_table('deliveries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('partner', sa.String(length=64), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('source_event_id', sa.String(length=128), nullable=True),
    sa.Column('external_id', sa.String(length=128), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('updated_at_raw', sa.String(length=64), nullable=True),
    sa.Column('updated_at_lo', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at_hi', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at_ambiguous', sa.Boolean(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=True),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('outcome_reason', sa.Text(), nullable=True),
    sa.Column('booking_id', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_deliveries_batch_id'), 'deliveries', ['batch_id'], unique=False)
    op.create_index(op.f('ix_deliveries_external_id'), 'deliveries', ['external_id'], unique=False)
    op.create_index(op.f('ix_deliveries_outcome'), 'deliveries', ['outcome'], unique=False)
    op.create_index(op.f('ix_deliveries_partner'), 'deliveries', ['partner'], unique=False)
    op.create_table('entitlements',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('booking_id', sa.BigInteger(), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('billable_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('billable_to', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("state in ('active','cancelled')", name='ck_entitlement_state'),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_id')
    )
    op.create_index(op.f('ix_entitlements_state'), 'entitlements', ['state'], unique=False)
    op.create_table('version_conflicts',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('booking_id', sa.BigInteger(), nullable=False),
    sa.Column('delivery_id', sa.BigInteger(), nullable=False),
    sa.Column('held_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('incoming_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('held_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('incoming_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('question_for_partner', sa.Text(), nullable=False),
    sa.Column('raised_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_version_conflicts_booking_id'), 'version_conflicts', ['booking_id'], unique=False)
    op.create_table('billing_lines',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('entitlement_id', sa.BigInteger(), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('cancellation', sa.String(length=24), nullable=False),
    sa.Column('charge_ref', sa.String(length=128), nullable=True),
    sa.Column('prepared_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['entitlement_id'], ['entitlements.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['billing_runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entitlement_id', 'period', name='uq_line_per_entitlement_period'),
    sa.UniqueConstraint('idempotency_key', name='uq_line_idempotency_key')
    )
    op.create_index('ix_line_state', 'billing_lines', ['state'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_line_state', table_name='billing_lines')
    op.drop_table('billing_lines')
    op.drop_index(op.f('ix_version_conflicts_booking_id'), table_name='version_conflicts')
    op.drop_table('version_conflicts')
    op.drop_index(op.f('ix_entitlements_state'), table_name='entitlements')
    op.drop_table('entitlements')
    op.drop_index(op.f('ix_deliveries_partner'), table_name='deliveries')
    op.drop_index(op.f('ix_deliveries_outcome'), table_name='deliveries')
    op.drop_index(op.f('ix_deliveries_external_id'), table_name='deliveries')
    op.drop_index(op.f('ix_deliveries_batch_id'), table_name='deliveries')
    op.drop_table('deliveries')
    op.drop_table('simulated_provider_charges')
    op.drop_index(op.f('ix_quarantine_items_partner'), table_name='quarantine_items')
    op.drop_index(op.f('ix_quarantine_items_external_id'), table_name='quarantine_items')
    op.drop_table('quarantine_items')
    op.drop_table('processed_events')
    op.drop_index(op.f('ix_journal_entries_outcome'), table_name='journal_entries')
    op.drop_index(op.f('ix_journal_entries_action'), table_name='journal_entries')
    op.drop_table('journal_entries')
    op.drop_index(op.f('ix_import_batches_partner'), table_name='import_batches')
    op.drop_table('import_batches')
    op.drop_table('bookings')
    op.drop_index(op.f('ix_billing_runs_period'), table_name='billing_runs')
    op.drop_table('billing_runs')
