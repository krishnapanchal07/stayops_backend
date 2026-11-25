"""Trusted database inventory boundary shared by inquiries and tenant allocation."""
from fastapi import HTTPException
from sqlalchemy import select
from app.modules.inventory.models import Room, Bed
from app.modules.inventory import management
from app.modules.properties.models import Property


def property_name(property_id, db=None):
    if db is None:
        raise RuntimeError('A database session is required for inventory.')
    item=db.get(Property,property_id)
    if not item:
        raise HTTPException(422,'Unknown property.')
    return item.name


def selection(room_id, bed_id=None, property_id=None, db=None):
    if db is None:
        raise RuntimeError('A database session is required for inventory.')
    item=db.scalar(management.loaded(select(Room).where(Room.id==room_id)))
    if not item:
        raise HTTPException(422,'Unknown room.')
    management.lock_property(db,item.floor.building.property_id)
    db.refresh(item)
    floor,building=item.floor,item.floor.building
    if item.status!='AVAILABLE' or floor.status!='ACTIVE' or building.status!='ACTIVE' or building.property.status!='ACTIVE':
        raise HTTPException(409,'Selected inventory is not active.')
    if property_id and building.property_id!=property_id:
        raise HTTPException(422,'Room does not belong to the selected property.')
    room=management.output(item,management.occupancy(db,[item.id]))
    # Eligibility uses operational state; occupancy is checked separately in available().
    states={b.id:b.status for b in item.beds}
    for b in room['beds']:
        b['status']=states[b['id']]
    bed=next((b for b in room['beds'] if b['id']==bed_id),None)
    if bed_id and not bed:
        raise HTTPException(422,'Bed does not belong to the selected room.')
    return room,bed


def available(db, room, bed, tenant_id=None):
    from app.modules.tenants.models import Occupancy
    beds = [bed] if bed else room['beds']
    occupied = dict(db.execute(select(Occupancy.bed_id, Occupancy.tenant_id).where(
        Occupancy.bed_id.in_([b['id'] for b in beds]))).all())
    if not any(b['status'] == 'AVAILABLE' and (b['id'] not in occupied or occupied[b['id']] == tenant_id) for b in beds):
        raise HTTPException(409, 'Selected bed or room is no longer available.')


def snapshot(room, bed=None, db=None):
    return {k: room[k] for k in ('propertyId', 'propertyName', 'buildingId', 'buildingName',
        'floorId', 'floorName', 'roomNumber', 'sharingType', 'securityDeposit')} | {
        'propertyName': property_name(room['propertyId'], db),
        'roomId': room['id'], 'bedId': bed['id'] if bed else None,
        'bedNumber': bed['bedNumber'] if bed else None, 'bedLabel': bed['label'] if bed else None,
        'monthlyRent': bed['rent'] if bed else room['monthlyRent'],
        'listingTitle': f"Room {room['roomNumber']}" + (f" · Bed {bed['bedNumber']}" if bed else '')}


def availability(db):
    from app.modules.tenants.models import Occupancy
    rows=db.execute(select(Room.id,Bed.id,management.available_expr()).select_from(Room).join(management.Floor).join(management.Building).join(Property).join(Bed).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(Bed.archived_at.is_(None))).all()
    result={}
    for room,bed,available in rows:
        result.setdefault(room,{})[bed]=bool(available)
    return result
