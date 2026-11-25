from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, require_permission, require_trusted_origin
from app.modules.inventory import management as m
from app.modules.inventory.models import Building, Floor, Room, Bed, RoomImage
from app.modules.inventory.schemas import BuildingWrite, FloorWrite, RoomWrite, BedWrite, RoomStatus, BedStatus, HierarchyStatus
from app.modules.properties import service as properties, images
from app.modules.properties.models import Property
from app.modules.properties.schemas import CoverInput, OrderInput
from app.modules.properties.storage import storage
from app.modules.tenants.models import Occupancy, Tenant

router=APIRouter(tags=['Inventory'])
read=Depends(require_permission('ROOM_VIEW'))
create=Depends(require_permission('ROOM_CREATE'))
edit=Depends(require_permission('ROOM_EDIT'))
trusted=[Depends(require_trusted_origin)]

def hierarchy_output(db,item):
    prop=item.property_id if isinstance(item,Building) else item.building.property_id
    buildings=m.hierarchy(db,[prop])[prop]
    return next(b for b in buildings if b['id']==item.id) if isinstance(item,Building) else next(f for b in buildings for f in b['floors'] if f['id']==item.id)

@router.get('/properties/{property_id}/buildings')
def buildings(property_id:str,db:Session=Depends(get_db),user=read):
    properties.get(db,property_id,user)
    return m.hierarchy(db,[property_id])[property_id]

@router.post('/properties/{property_id}/buildings',status_code=201,dependencies=trusted)
def create_building(property_id:str,payload:BuildingWrite,db:Session=Depends(get_db),user=create):
    properties.get(db,property_id,user,True)
    item=Building(property_id=property_id,name=payload.name,building_code=payload.buildingCode,description=payload.description,status=payload.status)
    db.add(item); db.commit()
    return hierarchy_output(db,item)

@router.get('/buildings/{key}')
def building(key:str,db:Session=Depends(get_db),user=read):
    return hierarchy_output(db,m.parent(db,Building,key,user))

def hierarchy_status(db,item,status):
    if status!='ACTIVE':
        stmt=select(Room.id).join(Floor).where(Floor.building_id==item.id) if isinstance(item,Building) else select(Room.id).where(Room.floor_id==item.id)
        m.assert_empty_occupancy(db,stmt)
    item.status=status

@router.patch('/buildings/{key}',dependencies=trusted)
def update_building(key:str,payload:BuildingWrite,db:Session=Depends(get_db),user=edit):
    item=m.parent(db,Building,key,user,True)
    hierarchy_status(db,item,payload.status)
    item.name,item.building_code,item.description=payload.name,payload.buildingCode,payload.description
    db.commit()
    return hierarchy_output(db,item)

@router.patch('/buildings/{key}/status',dependencies=trusted)
def building_status(key:str,payload:HierarchyStatus,db:Session=Depends(get_db),user=edit):
    item=m.parent(db,Building,key,user,True); hierarchy_status(db,item,payload.status); db.commit()
    return hierarchy_output(db,item)

@router.get('/buildings/{key}/floors')
def floors(key:str,db:Session=Depends(get_db),user=read):
    return hierarchy_output(db,m.parent(db,Building,key,user))['floors']

@router.post('/buildings/{key}/floors',status_code=201,dependencies=trusted)
def create_floor(key:str,payload:FloorWrite,db:Session=Depends(get_db),user=create):
    building=m.parent(db,Building,key,user,True)
    if building.status!='ACTIVE': raise HTTPException(409,'Building is not active.')
    item=Floor(building_id=key,floor_number=payload.floorNumber,name=payload.name,description=payload.description,status=payload.status)
    db.add(item); db.commit()
    return hierarchy_output(db,item)

@router.get('/floors/{key}')
def floor(key:str,db:Session=Depends(get_db),user=read):
    return hierarchy_output(db,m.parent(db,Floor,key,user))

@router.patch('/floors/{key}',dependencies=trusted)
def update_floor(key:str,payload:FloorWrite,db:Session=Depends(get_db),user=edit):
    item=m.parent(db,Floor,key,user,True); hierarchy_status(db,item,payload.status)
    item.name,item.floor_number,item.description=payload.name,payload.floorNumber,payload.description
    db.commit()
    return hierarchy_output(db,item)

@router.patch('/floors/{key}/status',dependencies=trusted)
def floor_status(key:str,payload:HierarchyStatus,db:Session=Depends(get_db),user=edit):
    item=m.parent(db,Floor,key,user,True); hierarchy_status(db,item,payload.status); db.commit()
    return hierarchy_output(db,item)

def page(db,user,request,offset,limit,public=False):
    stmt=m.base(None if public else user)
    params=request.query_params
    for column,key in ((Property.id,'propertyId'),(Building.id,'buildingId'),(Floor.id,'floorId'),(Room.room_type,'roomType'),(Room.sharing_type,'sharingType')):
        if params.get(key): stmt=stmt.where(column==params[key])
    if params.get('query'):
        needle='%'+params['query'][:120].replace('%',r'\%').replace('_',r'\_')+'%'
        stmt=stmt.where(or_(Room.room_number.ilike(needle),Property.name.ilike(needle),Building.name.ilike(needle)))
    occupied=select(func.count()).select_from(Occupancy).where(Occupancy.room_id==Room.id).correlate(Room).scalar_subquery()
    available=select(Bed.id).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(Bed.room_id==Room.id,m.available_expr()).correlate(Room,Floor,Building,Property).exists()
    state=params.get('status')
    if state in ('MAINTENANCE','BLOCKED','ARCHIVED'): stmt=stmt.where(Room.status==state)
    else:
        stmt=stmt.where(Room.archived_at.is_(None))
        if state=='FULL': stmt=stmt.where(occupied==Room.capacity)
        elif state=='PARTIALLY_OCCUPIED': stmt=stmt.where(occupied>0,occupied<Room.capacity)
        elif state=='AVAILABLE': stmt=stmt.where(occupied==0,Room.status=='AVAILABLE')
    if params.get('availability')=='HAS_AVAILABLE' or public: stmt=stmt.where(available)
    elif params.get('availability')=='NO_AVAILABLE': stmt=stmt.where(~available)
    total=db.scalar(select(func.count()).select_from(stmt.subquery()))
    sort={'roomNumber':Room.room_number,'property':Property.name,'location':Floor.name,'type':Room.room_type,'sharing':Room.sharing_type,'rent':Room.monthly_rent,'status':Room.status}.get(params.get('sortBy'),Room.room_number)
    rows=db.scalars(m.loaded(stmt.order_by(sort.desc() if params.get('sortDirection')=='desc' else sort.asc(),Room.id).offset(offset).limit(limit))).all()
    allocations=m.occupancy(db,[r.id for r in rows])
    return {'items':[m.output(r,allocations,public,False) for r in rows],'total':total,'offset':offset,'limit':limit}

@router.get('/rooms')
def rooms(request:Request,db:Session=Depends(get_db),user=read,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
    return page(db,user,request,offset,limit)

@router.get('/rooms/summary')
def summary(db:Session=Depends(get_db),user=read):
    return m.summary(db,m.base(user).where(Room.archived_at.is_(None)))

@router.get('/rooms/assignable-tenants')
def assignable(db:Session=Depends(get_db),user=Depends(require_permission('ROOM_ALLOCATE','TENANT_VIEW'))):
    from app.modules.inquiries.service import scoped
    stmt=scoped(select(Tenant),Tenant,user).where(Tenant.status.in_(['PROSPECT','ONBOARDING']),~select(Occupancy.bed_id).where(Occupancy.tenant_id==Tenant.id).exists())
    return [{'id':str(t.id),'name':f"{t.data['firstName']} {t.data['lastName']}"} for t in db.scalars(stmt.order_by(Tenant.created_at).limit(100))]

@router.get('/public/rooms')
def public_rooms(request:Request,db:Session=Depends(get_db),offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
    return page(db,None,request,offset,limit,True)

@router.get('/public/rooms/{key}')
def public_room(key:str,db:Session=Depends(get_db)):
    item=m.get(db,key)
    return m.output(item,m.occupancy(db,[key]),True)

@router.get('/public/available-beds')
def available_beds(request:Request,db:Session=Depends(get_db),offset:int=Query(0,ge=0),limit:int=Query(8,ge=1,le=100)):
    p=request.query_params
    stmt=m.base().join(Bed,Bed.room_id==Room.id).outerjoin(Occupancy,Occupancy.bed_id==Bed.id).where(m.available_expr())
    for column,key in ((Property.id,'propertyId'),(Room.room_type,'roomType'),(Room.sharing_type,'sharingType')):
        if p.get(key): stmt=stmt.where(column==p[key])
    if p.get('query'):
        needle='%'+p['query'][:120].replace('%',r'\%').replace('_',r'\_')+'%'
        stmt=stmt.where(or_(Room.room_number.ilike(needle),Property.name.ilike(needle),Property.city.ilike(needle)))
    rents={'UNDER_6000':Bed.rent<6000,'6000_8000':and_(Bed.rent>=6000,Bed.rent<=8000),'8000_10000':and_(Bed.rent>8000,Bed.rent<=10000),'ABOVE_10000':Bed.rent>10000}
    if p.get('rentRange') in rents: stmt=stmt.where(rents[p['rentRange']])
    total=db.scalar(select(func.count()).select_from(stmt.subquery()))
    pairs=db.execute(stmt.with_only_columns(Room.id,Bed.id).order_by(Property.name,Room.room_number,Bed.bed_number,Bed.id).offset(offset).limit(limit)).all()
    ids=[r for r,_ in pairs];bed_ids={b for _,b in pairs}
    rooms=db.scalars(m.loaded(m.base().where(Room.id.in_(ids)))).all()
    allocations=m.occupancy(db,ids)
    items=[]
    for room in sorted(rooms,key=lambda r:ids.index(r.id)):
        value=m.output(room,allocations,True,False)
        value['beds']=[b for b in value['beds'] if b['id'] in bed_ids]
        items.append(value)
    return {'items':items,'total':total,'offset':offset,'limit':limit}

@router.post('/rooms',status_code=201,dependencies=trusted)
def create_room(payload:RoomWrite,db:Session=Depends(get_db),user=create):
    item=m.write_room(db,payload,user)
    return m.output(item,m.occupancy(db,[item.id]))

@router.get('/rooms/{key}')
def room(key:str,db:Session=Depends(get_db),user=read):
    if user.role_id=='TENANT':
        tenant=db.scalar(select(Tenant).where(Tenant.user_id==user.id,Tenant.room_id==key))
        if not tenant: raise HTTPException(404,'Room not found.')
        item=db.scalar(m.loaded(select(Room).where(Room.id==key)))
        return m.output(item,m.occupancy(db,[key]),True)
    item=m.get(db,key,user)
    return m.output(item,m.occupancy(db,[key]))

@router.patch('/rooms/{key}',dependencies=trusted)
def update_room(key:str,payload:RoomWrite,db:Session=Depends(get_db),user=edit):
    item=m.write_room(db,payload,user,m.get(db,key,user,True))
    return m.output(item,m.occupancy(db,[key]))

@router.patch('/rooms/{key}/status',dependencies=trusted)
def room_status(key:str,payload:RoomStatus,db:Session=Depends(get_db),user=edit):
    item=m.get(db,key,user,True)
    if payload.status!='AVAILABLE': m.assert_empty_occupancy(db,[key])
    item.status=payload.status; item.archived_at=m.now() if payload.status=='ARCHIVED' else None
    db.commit()
    return m.output(item,m.occupancy(db,[key]))

@router.post('/rooms/{key}/archive',dependencies=trusted)
def archive(key:str,db:Session=Depends(get_db),user=edit):
    return room_status(key,RoomStatus(status='ARCHIVED'),db,user)

@router.get('/rooms/{key}/beds')
def beds(key:str,db:Session=Depends(get_db),user=read):
    return room(key,db,user)['beds']

@router.post('/rooms/{key}/beds',status_code=201,dependencies=trusted)
def create_bed(key:str,payload:BedWrite,db:Session=Depends(get_db),user=edit):
    item=m.get(db,key,user,True)
    if len([b for b in item.beds if not b.archived_at])>=item.capacity:
        raise HTTPException(409,'Increase room capacity before adding a bed.')
    item.beds.append(Bed(bed_number=payload.bedNumber,label=payload.label,rent=payload.rent,status=payload.status))
    db.commit()
    return m.output(item,m.occupancy(db,[key]))

def bed_parent(db,key,user):
    bed=db.get(Bed,key)
    if not bed or bed.archived_at: raise HTTPException(404,'Bed not found.')
    item=m.get(db,bed.room_id,user,True)
    if db.get(Occupancy,key): raise HTTPException(409,'Occupied beds can only be released through tenant checkout or reassignment.')
    return bed,item

@router.patch('/beds/{key}',dependencies=trusted)
def update_bed(key:str,payload:BedWrite,db:Session=Depends(get_db),user=edit):
    bed,item=bed_parent(db,key,user)
    bed.bed_number,bed.label,bed.rent,bed.status=payload.bedNumber,payload.label,payload.rent,payload.status
    db.commit(); return m.output(item,m.occupancy(db,[item.id]))

@router.patch('/beds/{key}/status',dependencies=trusted)
def bed_status(key:str,payload:BedStatus,db:Session=Depends(get_db),user=edit):
    bed,item=bed_parent(db,key,user); bed.status=payload.status
    db.commit(); return m.output(item,m.occupancy(db,[item.id]))

@router.post('/beds/{key}/release',dependencies=trusted)
def release_bed(key:str,db:Session=Depends(get_db),user=Depends(require_permission('TENANT_EDIT','ROOM_ALLOCATE'))):
    from app.modules.tenants import service as tenants
    entry=db.get(Occupancy,key)
    if not entry: raise HTTPException(409,'Bed no longer has an active allocation.')
    room_id=entry.room_id
    tenant=tenants.get(db,entry.tenant_id,user,True)
    item=m.get(db,room_id,user,True)
    if tenant.bed_id!=key: raise HTTPException(409,'Tenant allocation changed; refresh before checkout.')
    tenants.status(db,tenant,'CHECKED_OUT',user)
    db.commit()
    return m.output(item,m.occupancy(db,[room_id]))

@router.get('/rooms/{key}/images')
def gallery(key:str,db:Session=Depends(get_db),user=read):
    return [m.image_output(i) for i in m.get(db,key,user).images]

@router.post('/rooms/{key}/images',dependencies=trusted)
async def upload(key:str,request:Request,db:Session=Depends(get_db),user=edit):
    from app.core.rate_limit import limiter
    limiter.auth(request,'room-image-upload',str(user.id))
    m.get(db,key,user)
    async with request.form(max_files=12,max_fields=0,max_part_size=images.MAX_IMAGE_SIZE_BYTES) as form:
        files=form.getlist('files')
        if not files or any(not isinstance(f,UploadFile) for f in files) or any(k!='files' for k in form): raise HTTPException(422,'Select image files.')
        prepared=[]
        for file in files:
            content=await images.read_upload(file)
            result=await run_in_threadpool(images.process,content,file.filename,file.content_type)
            prepared.append((*result,len(content)))
        item=m.get(db,key,user,True)
        images.persist_batch(db,item,prepared,image_model=RoomImage)
        return [m.image_output(i) for i in item.images]

@router.delete('/rooms/{key}/images/{image_id}',dependencies=trusted)
def delete_image(key:str,image_id:UUID,db:Session=Depends(get_db),user=edit):
    item=m.get(db,key,user,True)
    image=next((i for i in item.images if i.id==image_id),None)
    if not image: raise HTTPException(404,'Image not found.')
    keys=[v['key'] for v in image.variants.values()]; item.images.remove(image)
    if image.is_cover and item.images: item.images[0].is_cover=True
    db.commit()
    for k in keys: storage.delete(k)
    return {'message':'Image deleted.'}

@router.patch('/rooms/{key}/cover-image',dependencies=trusted)
def cover(key:str,payload:CoverInput,db:Session=Depends(get_db),user=edit):
    item=m.get(db,key,user,True)
    if payload.imageId not in {i.id for i in item.images}: raise HTTPException(422,'Image does not belong to this room.')
    for i in item.images: i.is_cover=i.id==payload.imageId
    db.commit(); return [m.image_output(i) for i in item.images]

@router.patch('/rooms/{key}/images/order',dependencies=trusted)
def order(key:str,payload:OrderInput,db:Session=Depends(get_db),user=edit):
    item=m.get(db,key,user,True)
    if len(payload.imageIds)!=len(item.images) or set(payload.imageIds)!={i.id for i in item.images}: raise HTTPException(422,'Include each image exactly once.')
    for i in item.images: i.display_order=payload.imageIds.index(i.id)
    db.commit(); return [m.image_output(i) for i in sorted(item.images,key=lambda i:i.display_order)]

@router.get('/room-images/{image_id}/{variant}')
def image_file(image_id:UUID,variant:Literal['thumbnail','medium','large'],request:Request,db:Session=Depends(get_db)):
    import time,hmac
    image=db.get(RoomImage,image_id)
    if not image: raise HTTPException(404,'Image not found.')
    public=db.scalar(m.base().with_only_columns(Room.id).where(Room.id==image.room_id))
    if not public:
        expiry=request.query_params.get('expires',''); signature=request.query_params.get('signature','')
        if not expiry.isdigit() or int(expiry)<time.time() or not hmac.compare_digest(signature,properties.image_signature(image_id,expiry)): raise HTTPException(404,'Image not found.')
    path=storage.path(image.variants[variant]['key'])
    if not path.is_file(): raise HTTPException(404,'Image unavailable.')
    return FileResponse(path,media_type='image/webp',headers={'Cache-Control':'public, max-age=86400, immutable' if public else 'private, max-age=300','X-Content-Type-Options':'nosniff'})
