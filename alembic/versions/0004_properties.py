"""Canonical properties and optimized image metadata; preserve legacy identifiers."""
import json
from pathlib import Path
from alembic import op
import sqlalchemy as sa

revision = '0004_properties'
down_revision = '0003_staff_management'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('properties',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('name', sa.String(80), nullable=False), sa.Column('type', sa.String(16), nullable=False),
        sa.Column('owner_user_id', sa.Uuid(), sa.ForeignKey('users.id')),
        sa.Column('address', sa.String(160), nullable=False), sa.Column('locality', sa.String(100), nullable=False),
        sa.Column('city', sa.String(100), nullable=False), sa.Column('state', sa.String(100), nullable=False),
        sa.Column('postal_code', sa.String(16), nullable=False), sa.Column('country', sa.String(80), nullable=False),
        sa.Column('latitude', sa.Float()), sa.Column('longitude', sa.Float()),
        sa.Column('description', sa.String(500), nullable=False), sa.Column('contact_number', sa.String(20), nullable=False),
        sa.Column('email', sa.String(320), nullable=False), sa.Column('amenities', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('is_public', sa.Boolean(), nullable=False),
        sa.Column('is_demo', sa.Boolean(), nullable=False), sa.Column('seed_key', sa.String(80), unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False), sa.Column('archived_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE','MAINTENANCE','ARCHIVED')", name='valid_status'),
        sa.CheckConstraint("type IN ('PG','HOSTEL','CO_LIVING')", name='valid_type'),
        sa.CheckConstraint('(latitude IS NULL AND longitude IS NULL) OR (latitude BETWEEN -85.05112878 AND 85.05112878 AND longitude BETWEEN -180 AND 180)', name='coordinates'))
    for key in ('name','type','owner_user_id','city','status'):
        op.create_index('ix_properties_' + key, 'properties', [key])
    op.create_table('property_images',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('property_id', sa.String(64), sa.ForeignKey('properties.id'), nullable=False),
        sa.Column('storage_key', sa.String(80), nullable=False, unique=True),
        sa.Column('original_filename_display', sa.String(160), nullable=False),
        sa.Column('image_width', sa.Integer(), nullable=False), sa.Column('image_height', sa.Integer(), nullable=False),
        sa.Column('original_file_size', sa.Integer(), nullable=False), sa.Column('content_type', sa.String(32), nullable=False),
        sa.Column('content_digest', sa.String(64), nullable=False), sa.Column('display_order', sa.Integer(), nullable=False),
        sa.Column('is_cover', sa.Boolean(), nullable=False), sa.Column('variants', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint('property_id','content_digest'))
    op.create_index('ix_property_images_property_id', 'property_images', ['property_id'])
    bind = op.get_bind()
    table = sa.table('properties', *[sa.column(c, t) for c,t in [
        ('id',sa.String),('name',sa.String),('type',sa.String),('address',sa.String),('locality',sa.String),
        ('city',sa.String),('state',sa.String),('postal_code',sa.String),('country',sa.String),
        ('description',sa.String),('contact_number',sa.String),('email',sa.String),('amenities',sa.JSON),
        ('status',sa.String),('is_public',sa.Boolean),('is_demo',sa.Boolean),('created_at',sa.DateTime),('updated_at',sa.DateTime)]])
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc)
    catalog = json.loads(Path(__file__).with_name('0004_property_catalog.json').read_text())
    known = {p['id'] for p in catalog}
    refs = set(bind.execute(sa.text('SELECT property_id FROM auth_property_assignments UNION SELECT property_id FROM tenants UNION SELECT property_id FROM inquiries UNION SELECT property_id FROM tenant_occupancies')).scalars()) - {None}
    # Unknown references remain intact and private, explicitly needing reconciliation.
    for key in sorted(refs - known):
        catalog.append(dict(id=key,name='Unmapped legacy property',type='PG',address='Requires reconciliation',city='Unknown',state='Unknown',postalCode='',description='Existing reference preserved. Super Admin must reconcile metadata.',amenities=[],status='INACTIVE'))
    for p in catalog:
        bind.execute(table.insert().values(id=p['id'],name=p['name'],type=p['type'],address=p['address'],locality='',
            city=p['city'],state=p['state'],postal_code=p['postalCode'],country='India',description=p['description'],
            contact_number='',email='',amenities=p['amenities'],status=p['status'],is_public=False,is_demo=p['id'] in known,created_at=stamp,updated_at=stamp))
    # A single existing OWNER assignment is trusted ownership evidence; ambiguous ownership is not guessed.
    bind.execute(sa.text("UPDATE properties p SET owner_user_id = s.user_id FROM (SELECT a.property_id, min(a.user_id::text)::uuid user_id FROM auth_property_assignments a JOIN users u ON u.id=a.user_id WHERE u.role_id='OWNER' GROUP BY a.property_id HAVING count(*)=1) s WHERE p.id=s.property_id"))
    for table_name in ('auth_property_assignments','tenants','inquiries','tenant_occupancies'):
        op.create_foreign_key('fk_' + table_name + '_property_id_properties', table_name, 'properties', ['property_id'], ['id'])


def downgrade():
    for table in ('auth_property_assignments','tenants','inquiries','tenant_occupancies'):
        op.drop_constraint('fk_' + table + '_property_id_properties',table,type_='foreignkey')
    op.drop_table('property_images')
    op.drop_table('properties')
