"""Staff employment profiles; identity and effective scope reuse auth tables."""
from alembic import op
import sqlalchemy as sa

revision = '0003_staff_management'
down_revision = '0002_inquiries_tenants'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.schema.CreateSequence(sa.Sequence('staff_employee_code_seq', start=1001)))
    op.create_table('staff_profiles',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False, unique=True),
        sa.Column('employee_code', sa.String(40), nullable=False, unique=True,
            server_default=sa.text("'STF-' || nextval('staff_employee_code_seq')::text")),
        sa.Column('phone', sa.String(10), nullable=False),
        sa.Column('department', sa.String(24), nullable=False), sa.Column('designation', sa.String(120), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('joining_date', sa.Date(), nullable=False),
        sa.Column('address', sa.JSON(), nullable=False), sa.Column('emergency_contact', sa.JSON(), nullable=False),
        sa.Column('link_confirmed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE','ON_LEAVE')", name='valid_status'),
        sa.CheckConstraint("department IN ('OPERATIONS','FINANCE','SECURITY','MAINTENANCE')", name='valid_department'),
        sa.CheckConstraint("phone ~ '^[0-9]{10}$'", name='valid_phone'))
    for field in ('department', 'status', 'created_at'):
        op.create_index('ix_staff_profiles_' + field, 'staff_profiles', [field])
    op.create_table('staff_link_invitations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('staff_id', sa.Uuid(), sa.ForeignKey('staff_profiles.id'), nullable=False, unique=True),
        sa.Column('token_digest', sa.String(64), nullable=False, unique=True),
        sa.Column('expected_role', sa.String(16), nullable=False),
        sa.Column('proposed_property_ids', sa.JSON(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('used_at', sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table('staff_link_invitations')
    op.drop_table('staff_profiles')
    op.execute(sa.schema.DropSequence(sa.Sequence('staff_employee_code_seq')))
