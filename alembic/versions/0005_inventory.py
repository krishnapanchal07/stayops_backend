"""Canonical inventory preserving legacy IDs and the active occupancy ledger."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa
revision = '0005_inventory'
down_revision = '0004_properties'
branch_labels = None
depends_on = None

DDL = ["CREATE TABLE buildings (\n\tid VARCHAR(64) NOT NULL, \n\tproperty_id VARCHAR(64) NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tbuilding_code VARCHAR(100) NOT NULL, \n\tdescription VARCHAR(1000) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_buildings PRIMARY KEY (id), \n\tCONSTRAINT uq_buildings_property_id UNIQUE (property_id, name), \n\tCONSTRAINT uq_buildings_property_code UNIQUE (property_id, building_code), \n\tCONSTRAINT ck_buildings_valid_status CHECK (status IN ('ACTIVE','INACTIVE','ARCHIVED')), \n\tCONSTRAINT fk_buildings_property_id_properties FOREIGN KEY(property_id) REFERENCES properties (id)\n)", 'CREATE INDEX ix_buildings_property_id ON buildings (property_id)', "CREATE TABLE floors (\n\tid VARCHAR(64) NOT NULL, \n\tbuilding_id VARCHAR(64) NOT NULL, \n\tfloor_number INTEGER NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tdescription VARCHAR(1000) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_floors PRIMARY KEY (id), \n\tCONSTRAINT uq_floors_building_id UNIQUE (building_id, floor_number), \n\tCONSTRAINT ck_floors_valid_status CHECK (status IN ('ACTIVE','INACTIVE','ARCHIVED')), \n\tCONSTRAINT fk_floors_building_id_buildings FOREIGN KEY(building_id) REFERENCES buildings (id)\n)", 'CREATE INDEX ix_floors_building_id ON floors (building_id)', "CREATE TABLE rooms (\n\tid VARCHAR(64) NOT NULL, \n\tfloor_id VARCHAR(64) NOT NULL, \n\troom_number VARCHAR(40) NOT NULL, \n\troom_type VARCHAR(16) NOT NULL, \n\tsharing_type VARCHAR(16) NOT NULL, \n\tcapacity INTEGER NOT NULL, \n\tmonthly_rent NUMERIC(12, 2) NOT NULL, \n\tsecurity_deposit NUMERIC(12, 2) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tamenities JSON NOT NULL, \n\tdescription VARCHAR(2000) NOT NULL, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_rooms PRIMARY KEY (id), \n\tCONSTRAINT uq_rooms_floor_id UNIQUE (floor_id, room_number), \n\tCONSTRAINT ck_rooms_valid_capacity CHECK (capacity BETWEEN 1 AND 4), \n\tCONSTRAINT ck_rooms_valid_money CHECK (monthly_rent > 0 AND security_deposit >= 0), \n\tCONSTRAINT ck_rooms_valid_type CHECK (room_type IN ('STANDARD','PREMIUM','DELUXE')), \n\tCONSTRAINT ck_rooms_valid_sharing CHECK (sharing_type IN ('1_SHARING','2_SHARING','3_SHARING','4_SHARING')), \n\tCONSTRAINT ck_rooms_sharing_capacity CHECK (capacity = CAST(substring(sharing_type,1,1) AS INTEGER)), \n\tCONSTRAINT ck_rooms_valid_status CHECK (status IN ('AVAILABLE','MAINTENANCE','BLOCKED','ARCHIVED')), \n\tCONSTRAINT fk_rooms_floor_id_floors FOREIGN KEY(floor_id) REFERENCES floors (id)\n)", 'CREATE INDEX ix_rooms_status ON rooms (status)', 'CREATE INDEX ix_rooms_floor_id ON rooms (floor_id)', "CREATE TABLE beds (\n\tid VARCHAR(64) NOT NULL, \n\troom_id VARCHAR(64) NOT NULL, \n\tbed_number VARCHAR(16) NOT NULL, \n\tlabel VARCHAR(80) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\trent NUMERIC(12, 2) NOT NULL, \n\tarchived_at TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_beds PRIMARY KEY (id), \n\tCONSTRAINT uq_beds_room_id UNIQUE (room_id, bed_number), \n\tCONSTRAINT uq_beds_id UNIQUE (id, room_id), \n\tCONSTRAINT ck_beds_valid_rent CHECK (rent > 0), \n\tCONSTRAINT ck_beds_valid_status CHECK (status IN ('AVAILABLE','RESERVED','MAINTENANCE','BLOCKED')), \n\tCONSTRAINT fk_beds_room_id_rooms FOREIGN KEY(room_id) REFERENCES rooms (id)\n)", 'CREATE INDEX ix_beds_room_id ON beds (room_id)', 'CREATE TABLE room_images (\n\tid UUID NOT NULL, \n\troom_id VARCHAR(64) NOT NULL, \n\tstorage_key VARCHAR(80) NOT NULL, \n\toriginal_filename_display VARCHAR(160) NOT NULL, \n\timage_width INTEGER NOT NULL, \n\timage_height INTEGER NOT NULL, \n\toriginal_file_size INTEGER NOT NULL, \n\tcontent_type VARCHAR(32) NOT NULL, \n\tcontent_digest VARCHAR(64) NOT NULL, \n\tdisplay_order INTEGER NOT NULL, \n\tis_cover BOOLEAN NOT NULL, \n\tvariants JSON NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_room_images PRIMARY KEY (id), \n\tCONSTRAINT uq_room_images_room_id UNIQUE (room_id, content_digest), \n\tCONSTRAINT fk_room_images_room_id_rooms FOREIGN KEY(room_id) REFERENCES rooms (id), \n\tCONSTRAINT uq_room_images_storage_key UNIQUE (storage_key)\n)', 'CREATE INDEX ix_room_images_room_id ON room_images (room_id)']

def upgrade():
    for statement in DDL:
        op.execute(statement)
    import_inventory()

def import_inventory():
    bind = op.get_bind()
    metadata = sa.MetaData()
    tables = {name:sa.Table(name,metadata,autoload_with=bind) for name in ('buildings','floors','rooms','beds')}
    catalog = json.loads(Path(__file__).with_name('0005_inventory_catalog.json').read_text())
    stamp = datetime.now(timezone.utc)
    buildings, floors, rooms, beds = {}, {}, {}, {}
    def unique(target, key, value):
        if key in target and target[key] != value:
            raise RuntimeError('Ambiguous legacy inventory identifier: '+key)
        target[key] = value
    for r in catalog:
        unique(buildings,r['buildingId'],dict(id=r['buildingId'],property_id=r['propertyId'],name=r['buildingName'],building_code=r['buildingId'],description='',status='ACTIVE',created_at=stamp,updated_at=stamp))
        match = re.search(r'-?\d+',r['floorName'])
        if not match:
            raise RuntimeError('Floor number needs reconciliation: '+r['floorId'])
        unique(floors,r['floorId'],dict(id=r['floorId'],building_id=r['buildingId'],floor_number=int(match.group()),name=r['floorName'],description='',status='ACTIVE',created_at=stamp,updated_at=stamp))
        unique(rooms,r['id'],dict(id=r['id'],floor_id=r['floorId'],room_number=r['roomNumber'],room_type=r['roomType'],sharing_type=r['sharingType'],capacity=r['capacity'],monthly_rent=r['monthlyRent'],security_deposit=r['securityDeposit'],status=r['status'] if r['status'] in ('MAINTENANCE','BLOCKED') else 'AVAILABLE',amenities=r['amenities'],description=r['description'],created_at=stamp,updated_at=stamp))
        for b in r['beds']:
            unique(beds,b['id'],dict(id=b['id'],room_id=r['id'],bed_number=b['bedNumber'],label=b['label'],rent=b['rent'],status=b['status'] if b['status'] in ('RESERVED','MAINTENANCE','BLOCKED') else 'AVAILABLE',created_at=stamp,updated_at=stamp))
    # Never invent mappings or tenants. Abort atomically before adding foreign keys.
    for table in ('inquiries','tenants','tenant_occupancies'):
        for prop, room, bed in bind.execute(sa.text(f'SELECT property_id, room_id, bed_id FROM {table}')):
            if room and room not in rooms or bed and bed not in beds:
                raise RuntimeError(f'Unmapped inventory reference in {table}: room={room}, bed={bed}')
            if bed and (not room or beds[bed]['room_id'] != room):
                raise RuntimeError(f'Incompatible room/bed reference in {table}: {bed}')
            if room:
                parent = buildings[floors[rooms[room]['floor_id']]['building_id']]['property_id']
                if prop != parent:
                    raise RuntimeError(f'Incompatible property/room reference in {table}: {room}')
    for name, rows in (('buildings',buildings),('floors',floors),('rooms',rooms),('beds',beds)):
        if rows:
            bind.execute(tables[name].insert(),list(rows.values()))
    for table in ('inquiries','tenants','tenant_occupancies'):
        op.create_foreign_key(f'fk_{table}_room_id_rooms',table,'rooms',['room_id'],['id'])
        op.create_foreign_key(f'fk_{table}_bed_room_beds',table,'beds',['bed_id','room_id'],['id','room_id'])
    # Cross-table relationship guard complements FKs without duplicating occupancy.
    op.execute('''CREATE FUNCTION inventory_occupancy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM beds b JOIN rooms r ON r.id=b.room_id JOIN floors f ON f.id=r.floor_id JOIN buildings g ON g.id=f.building_id
        WHERE b.id=NEW.bed_id AND r.id=NEW.room_id AND g.property_id=NEW.property_id) THEN
        RAISE EXCEPTION 'Invalid occupancy relationship' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$''')
    op.execute('CREATE TRIGGER inventory_occupancy_relationship BEFORE INSERT OR UPDATE ON tenant_occupancies FOR EACH ROW EXECUTE FUNCTION inventory_occupancy_guard()')

def downgrade():
    from app.core.config import get_settings
    if get_settings().app_env != 'test':
        raise RuntimeError('Inventory downgrade is destructive. Restore a verified pre-migration database AND media backup instead; never discard live allocations.')
    op.execute('DROP TRIGGER inventory_occupancy_relationship ON tenant_occupancies')
    op.execute('DROP FUNCTION inventory_occupancy_guard()')
    for table in ('inquiries','tenants','tenant_occupancies'):
        op.drop_constraint(f'fk_{table}_room_id_rooms',table,type_='foreignkey')
        op.drop_constraint(f'fk_{table}_bed_room_beds',table,type_='foreignkey')
    for table in ('room_images','beds','rooms','floors','buildings'):
        op.drop_table(table)
