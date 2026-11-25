from fastapi import HTTPException
from sqlalchemy import select, func, case, and_, or_
from sqlalchemy.orm import joinedload, selectinload
from app.core.security import now
from app.modules.inventory.models import Building, Floor, Room, Bed
from app.modules.properties.models import Property
from app.modules.properties import service as properties
from app.modules.tenants.models import Occupancy, Tenant


def base(user=None):
    stmt = select(Room).join(Floor).join(Building).join(Property)
    return properties.scoped(stmt,user) if user else properties.public_scope(stmt).where(
        Building.status=='ACTIVE',Floor.status=='ACTIVE',Room.status=='AVAILABLE')

def loaded(stmt):
    return stmt.options(joinedload(Room.floor).joinedload(Floor.building).joinedload(Building.property),selectinload(Room.beds),selectinload(Room.images))

def get(db, key, user=None, lock=False):
    item = db.scalar(loaded(base(user).where(Room.id==key)))
    if not item:
        raise HTTPException(404,'Room not found or access denied.')
    if lock:
        lock_property(db,item.floor.building.property_id)
        db.refresh(item)
    return item

def lock_property(db, key):
    db.scalar(select(Property).where(Property.id==key).with_for_update())

def parent(db, model, key, user, lock=False):
    item = db.get(model,key)
    if not item:
        raise HTTPException(404,'Building or floor not found.')
    prop = item.property_id if model is Building else item.building.property_id
    properties.get(db,prop,user,lock)
    return item

def occupancy(db, ids):
    if not ids:
        return {}
    return {o.bed_id:(o,t) for o,t in db.execute(select(Occupancy,Tenant).join(Tenant,Tenant.id==Occupancy.tenant_id).where(Occupancy.room_id.in_(ids)))}

def available_expr():
    return and_(Bed.archived_at.is_(None),Bed.status=='AVAILABLE',Occupancy.bed_id.is_(None),Room.status=='AVAILABLE',Floor.status=='ACTIVE',Building.status=='ACTIVE',Property.status=='ACTIVE')

def image_output(image):
    result = properties.image_output(image)
    return {**result,**{k:result[k].replace('/property-images/','/room-images/') for k in ('thumbnail','medium','large')}}

def output(item, allocations=None, public=False, detail=True):
    allocations = allocations or {}
    floor, building = item.floor,item.floor.building
    prop = building.property
    active = item.status=='AVAILABLE' and floor.status=='ACTIVE' and building.status=='ACTIVE' and prop.status=='ACTIVE'
    beds=[]
    for b in item.beds:
        if b.archived_at:
            continue
        allocation = allocations.get(b.id)
        tenant = allocation[1] if allocation else None
        state = 'OCCUPIED' if allocation else b.status if active else 'BLOCKED'
        beds.append(dict(id=b.id,roomId=item.id,bedNumber=b.bed_number,label=b.label,status=state,rent=float(b.rent),
            tenantId=str(tenant.id) if tenant and not public else None,
            tenantName=f"{tenant.data['firstName']} {tenant.data['lastName']}" if tenant and not public else None,
            moveInDate=tenant.data.get('moveInDate') if tenant and not public else None))
    occupied=sum(b['status']=='OCCUPIED' for b in beds)
    available=sum(b['status']=='AVAILABLE' for b in beds)
    status=item.status if item.status!='AVAILABLE' else ('BLOCKED' if not active else 'FULL' if occupied==len(beds) and beds else 'PARTIALLY_OCCUPIED' if occupied else 'AVAILABLE' if available else 'BLOCKED')
    images=[image_output(i) for i in item.images]
    images=images if detail else [i for i in images if i['isCover']][:1]
    return dict(id=item.id,propertyId=prop.id,propertyName=prop.name,buildingId=building.id,buildingName=building.name,
        floorId=floor.id,floorName=floor.name,roomNumber=item.room_number,roomType=item.room_type,sharingType=item.sharing_type,
        capacity=item.capacity,monthlyRent=float(item.monthly_rent),securityDeposit=float(item.security_deposit),status=status,
        amenities=item.amenities,description=item.description,beds=beds,images=[dict(**i,url=i['medium'],isPrimary=i['isCover'],sortOrder=i['displayOrder'],alt=i['filename']) for i in images],
        occupiedBeds=occupied,availableBeds=available,reservedBeds=sum(b['status']=='RESERVED' for b in beds),
        unavailableBeds=sum(b['status'] in ('BLOCKED','MAINTENANCE') for b in beds),occupancyPercent=round(occupied/len(beds)*100) if beds else 0,
        createdAt=item.created_at.isoformat(),updatedAt=item.updated_at.isoformat())

def hierarchy(db, property_ids):
    """One bounded hierarchy query plus grouped SQL counts, never fixture counters."""
    buildings=db.scalars(select(Building).where(Building.property_id.in_(property_ids)).options(selectinload(Building.floors)).order_by(Building.name).execution_options(populate_existing=True)).all()
    stats=db.execute(select(Floor.id,func.count(func.distinct(Room.id)),func.count(Bed.id),func.count(Occupancy.bed_id),
        func.count().filter(available_expr())).select_from(Floor).join(Building).join(Property).outerjoin(Room,and_(Room.floor_id==Floor.id,Room.archived_at.is_(None))).outerjoin(Bed,and_(Bed.room_id==Room.id,Bed.archived_at.is_(None))).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(Building.property_id.in_(property_ids)).group_by(Floor.id)).all()
    counts={r[0]:r[1:] for r in stats}
    result={p:[] for p in property_ids}
    for b in buildings:
        floors=[]
        for f in sorted(b.floors,key=lambda f:f.floor_number):
            rooms,beds,occupied,available=counts.get(f.id,(0,0,0,0))
            floors.append(dict(id=f.id,buildingId=b.id,name=f.name,floorNumber=f.floor_number,description=f.description,status=f.status,roomCount=rooms,bedCount=beds,occupiedBeds=occupied,availableBeds=available))
        result[b.property_id].append(dict(id=b.id,propertyId=b.property_id,name=b.name,buildingCode=b.building_code,description=b.description,status=b.status,floors=floors))
    return result

def assert_empty_occupancy(db, room_ids):
    occupied=db.scalar(select(Occupancy.bed_id).where(Occupancy.room_id.in_(room_ids)).limit(1))
    reserved=db.scalar(select(Bed.id).where(Bed.room_id.in_(room_ids),Bed.archived_at.is_(None),Bed.status=='RESERVED').limit(1))
    if occupied or reserved:
        raise HTTPException(409,'Release occupied or reserved beds before deactivating inventory.')

def write_room(db, payload, user, item=None):
    floor=parent(db,Floor,payload.floorId,user,True)
    if floor.building_id!=payload.buildingId or floor.building.property_id!=payload.propertyId:
        raise HTTPException(422,'Property, building and floor must match.')
    if floor.status!='ACTIVE' or floor.building.status!='ACTIVE' or floor.building.property.status!='ACTIVE':
        raise HTTPException(409,'Select an active property, building and floor.')
    if item and item.floor_id!=floor.id:
        raise HTTPException(422,'Moving existing rooms is not supported; preserve their references.')
    if item and payload.status!='AVAILABLE':
        assert_empty_occupancy(db,[item.id])
    previous_rent=item.monthly_rent if item else None
    item=item or Room(floor_id=floor.id)
    for key,field in {'roomNumber':'room_number','roomType':'room_type','sharingType':'sharing_type','monthlyRent':'monthly_rent','securityDeposit':'security_deposit','capacity':'capacity','status':'status','amenities':'amenities','description':'description'}.items():
        setattr(item,field,getattr(payload,key))
    item.archived_at=None
    db.add(item)
    db.flush()
    active=[b for b in item.beds if not b.archived_at]
    # Preserve explicit bed overrides; standard beds follow the room tariff.
    for bed in active:
        if previous_rent is not None and bed.rent==previous_rent:
            bed.rent=item.monthly_rent
    if len(active)>item.capacity:
        removing=active[item.capacity:]
        if any(b.status!='AVAILABLE' for b in removing) or db.scalar(select(Occupancy.bed_id).where(Occupancy.bed_id.in_([b.id for b in removing])).limit(1)):
            raise HTTPException(409,'Capacity reduction would remove an occupied or reserved bed.')
        for b in removing:
            b.archived_at=now()
    for index in range(len(active),item.capacity):
        number=str(index+1)
        while any(b.bed_number==number for b in item.beds):
            number=str(int(number)+1)
        item.beds.append(Bed(bed_number=number,label='Bed '+number,rent=item.monthly_rent,status='AVAILABLE'))
    db.commit()
    return item

def summary(db, stmt):
    ids=stmt.with_only_columns(Room.id).order_by(None).subquery()
    counts=select(Room.id.label('id'),Room.status.label('operational'),func.count(Bed.id).label('beds'),func.count(Occupancy.bed_id).label('occupied'),
        func.count().filter(available_expr()).label('available'),func.count().filter(Bed.status=='RESERVED').label('reserved')).join(Floor).join(Building).join(Property).outerjoin(Bed,and_(Bed.room_id==Room.id,Bed.archived_at.is_(None))).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(Room.id.in_(select(ids.c.id))).group_by(Room.id).subquery()
    row=db.execute(select(func.count(),func.coalesce(func.sum(counts.c.beds),0),func.coalesce(func.sum(counts.c.occupied),0),func.coalesce(func.sum(counts.c.available),0),func.count().filter(counts.c.available>0),func.count().filter(and_(counts.c.occupied==counts.c.beds,counts.c.beds>0)),func.count().filter(counts.c.operational=='MAINTENANCE'),func.coalesce(func.sum(counts.c.reserved),0),func.count().filter(and_(counts.c.occupied>0,counts.c.occupied<counts.c.beds)))).one()
    total,beds,occupied,available,available_rooms,full,maintenance,reserved,partial=row
    return dict(totalRooms=total,totalBeds=beds,occupiedBeds=occupied,availableBeds=available,availableRooms=available_rooms,fullRooms=full,maintenanceRooms=maintenance,reservedBeds=reserved,partiallyOccupiedRooms=partial,occupancyPercent=round(occupied/beds*100) if beds else 0)
