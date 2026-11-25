"""Persistent inquiries, tenant profiles and transitional allocation ledger.

No fixture residents/accounts are imported; property references have no fictional FKs.
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_inquiries_tenants'
down_revision = '0001_auth'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('inquiries',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id')),
        sa.Column('property_id', sa.String(64)), sa.Column('room_id', sa.String(64)), sa.Column('bed_id', sa.String(64)),
        sa.Column('status', sa.String(24), nullable=False), sa.Column('tenant_id', sa.Uuid(), unique=True),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('idempotency_key', sa.String(64), unique=True), sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('SUBMITTED','CONTACTED','SHORTLISTED','APPROVED','CONVERTED','REJECTED','WITHDRAWN')", name='valid_status'),
        sa.CheckConstraint('email = lower(trim(email))', name='email_normalized'),
        sa.CheckConstraint("(kind = 'ROOM' AND property_id IS NOT NULL AND room_id IS NOT NULL) OR (kind IN ('tenant','owner','staff') AND property_id IS NULL AND room_id IS NULL AND bed_id IS NULL)", name='kind_reference'))
    for field in ('email', 'user_id', 'property_id', 'room_id', 'status', 'created_at'):
        op.create_index('ix_inquiries_' + field, 'inquiries', [field])
    op.create_index('uq_inquiries_open_target', 'inquiries', ['email', 'room_id', sa.text("coalesce(bed_id, '')")],
        unique=True, postgresql_where=sa.text("status IN ('SUBMITTED','CONTACTED','SHORTLISTED','APPROVED')"))
    op.create_table('tenants',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('tenant_code', sa.String(40), unique=True, nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), unique=True),
        sa.Column('email', sa.String(320), nullable=False), sa.Column('property_id', sa.String(64)),
        sa.Column('room_id', sa.String(64)), sa.Column('bed_id', sa.String(64)),
        sa.Column('status', sa.String(24), nullable=False), sa.Column('source', sa.String(16), nullable=False),
        sa.Column('inquiry_id', sa.Uuid(), unique=True), sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('internal_notes', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('PROSPECT','ONBOARDING','ACTIVE','NOTICE_PERIOD','CHECKED_OUT','INACTIVE')", name='valid_status'),
        sa.CheckConstraint("source IN ('DIRECT','WALK_IN','INQUIRY')", name='valid_source'),
        sa.CheckConstraint('email = lower(trim(email))', name='email_normalized'))
    for field in ('email', 'property_id', 'room_id', 'status', 'created_at'):
        op.create_index('ix_tenants_' + field, 'tenants', [field], unique=field == 'email')
    op.create_foreign_key('fk_inquiries_tenant_id_tenants', 'inquiries', 'tenants', ['tenant_id'], ['id'])
    op.create_foreign_key('fk_tenants_inquiry_id_inquiries', 'tenants', 'inquiries', ['inquiry_id'], ['id'])
    op.create_table('inquiry_status_events',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('inquiry_id', sa.Uuid(), sa.ForeignKey('inquiries.id'), nullable=False),
        sa.Column('previous', sa.String(24)), sa.Column('status', sa.String(24), nullable=False),
        sa.Column('actor_user_id', sa.Uuid(), sa.ForeignKey('users.id')), sa.Column('actor_name', sa.String(120), nullable=False),
        sa.Column('note', sa.Text()), sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_inquiry_status_events_inquiry_id', 'inquiry_status_events', ['inquiry_id'])
    op.create_table('tenant_occupancies',
        sa.Column('bed_id', sa.String(64), primary_key=True),
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), nullable=False, unique=True),
        sa.Column('room_id', sa.String(64), nullable=False), sa.Column('property_id', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    for field in ('room_id', 'property_id'):
        op.create_index('ix_tenant_occupancies_' + field, 'tenant_occupancies', [field])
    op.create_table('tenant_activity',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('actor_user_id', sa.Uuid(), sa.ForeignKey('users.id')), sa.Column('actor_name', sa.String(120), nullable=False),
        sa.Column('action', sa.String(80), nullable=False), sa.Column('description', sa.String(2000), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_tenant_activity_tenant_id', 'tenant_activity', ['tenant_id'])
    op.create_table('tenant_invitations',
        sa.Column('id', sa.Uuid(), primary_key=True), sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('token_digest', sa.String(64), nullable=False, unique=True), sa.Column('email', sa.String(320), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('used_at', sa.DateTime(timezone=True)))
    op.create_index('ix_tenant_invitations_tenant_id', 'tenant_invitations', ['tenant_id'])


def downgrade():
    # Explicit rollback removes only Phase 2 tables; never touches auth/session data.
    for table in ('tenant_invitations', 'tenant_activity', 'tenant_occupancies', 'inquiry_status_events'):
        op.drop_table(table)
    op.drop_constraint('fk_inquiries_tenant_id_tenants', 'inquiries', type_='foreignkey')
    op.drop_constraint('fk_tenants_inquiry_id_inquiries', 'tenants', type_='foreignkey')
    op.drop_table('tenants')
    op.drop_table('inquiries')
