"""Paged public properties filtered by eligible canonical beds."""
from math import isfinite
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import select, func, or_, and_, cast, String
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.modules.inventory import management as m
from app.modules.inventory.models import Room, Bed, Floor, Building
from app.modules.properties.models import Property
from app.modules.properties import service as properties
from app.modules.tenants.models import Occupancy

router=APIRouter()

@router.get('/public/discovery')
def discovery(request:Request,db:Session=Depends(get_db),offset:int=Query(0,ge=0),limit:int=Query(4,ge=1,le=20)):
    p=request.query_params
    eligible=select(Bed.id.label('bed_id'),Room.id.label('room_id'),Building.property_id.label('property_id'),Bed.rent.label('rent')).select_from(Bed).join(Room).join(Floor).join(Building).join(Property).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(m.available_expr())
    for column,key in ((Room.room_type,'roomType'),(Room.sharing_type,'sharingType')):
        if p.get(key): eligible=eligible.where(column==p[key])
    rents={'UNDER_6000':Bed.rent<6000,'6000_8000':and_(Bed.rent>=6000,Bed.rent<=8000),'8000_10000':and_(Bed.rent>8000,Bed.rent<=10000),'ABOVE_10000':Bed.rent>10000}
    if p.get('rentRange') in rents: eligible=eligible.where(rents[p['rentRange']])
    for amenity in p.getlist('amenity')[:30]:
        eligible=eligible.where(or_(cast(Room.amenities,String).ilike('%"'+amenity.replace('%','').replace('_','')+'"%'),cast(Property.amenities,String).ilike('%"'+amenity.replace('%','').replace('_','')+'"%')))
    e=eligible.subquery()
    stats=select(e.c.property_id,func.count().label('availableBeds'),func.count(func.distinct(e.c.room_id)).label('availableRooms'),func.min(e.c.rent).label('rent')).group_by(e.c.property_id).subquery()
    stmt=properties.public_scope(select(Property).join(stats,stats.c.property_id==Property.id))
    for column,key in ((Property.id,'propertyId'),(Property.city,'city'),(Property.type,'propertyType')):
        if p.get(key): stmt=stmt.where(column==p[key])
    if p.get('locality'): stmt=stmt.where(func.trim(func.regexp_replace(Property.address,'^.*,',''))==p['locality'])
    if p.get('gender'): stmt=stmt.where(False) # No invented gender policy.
    if p.get('query'):
        needle='%'+p['query'][:120].replace('%',r'\%').replace('_',r'\_')+'%'
        stmt=stmt.where(or_(Property.name.ilike(needle),Property.address.ilike(needle),Property.city.ilike(needle)))
    if p.get('bounds'):
        try:
            south,west,north,east=map(float,p['bounds'].split(','))
            if not all(map(isfinite,(south,west,north,east))) or not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
                raise ValueError('Invalid bounds')
            stmt=stmt.where(Property.latitude.between(south,north),Property.longitude.between(west,east))
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(422,'Invalid map bounds.') from None
    total=db.scalar(select(func.count()).select_from(stmt.subquery()))
    sort=stats.c.rent.asc() if p.get('sort')=='rent-asc' else stats.c.rent.desc() if p.get('sort')=='rent-desc' else Property.name.asc()
    selected=db.scalars(stmt.order_by(sort,Property.id).offset(offset).limit(limit)).all()
    ids=[i.id for i in selected]
    # Counts remain SQL aggregates; bounded previews avoid downloading full galleries/inventory.
    rows=db.scalars(m.loaded(m.base().where(Property.id.in_(ids),Room.id.in_(select(e.c.room_id))).order_by(Room.id).limit(400))).all()
    occupied=m.occupancy(db,[r.id for r in rows])
    eligible_ids=set(db.scalars(select(e.c.bed_id).where(e.c.property_id.in_(ids))))
    room_values=[]
    for r in rows:
        value=m.output(r,occupied,True,False)
        value['beds']=[b for b in value['beds'] if b['id'] in eligible_ids]
        room_values.append(value)
    counts={r.property_id:{'availableBedCount':r.availableBeds,'availableRoomCount':r.availableRooms,'startingRent':float(r.rent)} for r in db.execute(select(stats).where(stats.c.property_id.in_(ids)))}
    return {'properties':[properties.output(i,public=True,detail=False) for i in selected],'rooms':room_values,'counts':counts,'total':total,'offset':offset,'limit':limit}

@router.get('/public/discovery/options')
def options(db:Session=Depends(get_db)):
    stmt=properties.public_scope(select(Property.city,func.trim(func.regexp_replace(Property.address,'^.*,','')).label('locality'))).distinct().limit(1000)
    rows=db.execute(stmt).all()
    return {'cities':sorted({r.city for r in rows}),'localities':sorted({r.locality for r in rows})}
