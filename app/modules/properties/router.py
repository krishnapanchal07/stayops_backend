from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from app.db.session import get_db
from app.core.config import get_settings
from app.modules.auth.dependencies import get_active_user, require_permission, require_trusted_origin
from app.modules.auth.models import User
from app.modules.properties import service, images
from app.modules.properties.models import Property, PropertyImage
from app.modules.properties.schemas import PropertyWrite, CoverInput, OrderInput, StatusInput
from app.modules.properties.storage import storage

router = APIRouter(tags=['Properties'])


def page(db, user, query, city, status, type, owner, offset, limit, sort, direction, public=False):
    stmt = service.public_scope(select(Property)) if public else service.scoped(select(Property),user)
    if query.strip():
        needle = '%' + query.strip().replace('%',r'\%').replace('_',r'\_') + '%'
        stmt = stmt.where(or_(Property.name.ilike(needle),Property.address.ilike(needle),Property.city.ilike(needle)))
    for column,value in ((Property.city,city),(Property.status,status),(Property.type,type),(Property.owner_user_id,owner)):
        if value:
            stmt = stmt.where(column == value)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    column = {'name':Property.name,'city':Property.city,'type':Property.type,'status':Property.status,'createdAt':Property.created_at}[sort]
    rows = db.scalars(stmt.order_by(column.asc() if direction == 'asc' else column.desc(),Property.id).offset(offset).limit(limit)).all()
    counts = service.occupancy_counts(db) if not public else {}
    return {'items':[service.output(p,counts,public,False) for p in rows],'total':total,'offset':offset,'limit':limit}


@router.get('/properties')
def listing(db: Session=Depends(get_db), user=Depends(get_active_user), query: str=Query('',max_length=120), city: str='', status: str='', type: str='', ownerUserId: UUID|None=None,
            offset: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),sortBy: Literal['name','city','type','status','createdAt']='name',sortDirection: Literal['asc','desc']='asc'):
    return page(db,user,query,city,status,type,ownerUserId,offset,limit,sortBy,sortDirection)


@router.get('/properties/summary')
def summary(db: Session=Depends(get_db),user=Depends(get_active_user)):
    rows = db.scalars(service.scoped(select(Property),user)).all()
    counts = service.occupancy_counts(db)
    values = [service.output(p,counts,detail=False) for p in rows]
    beds = sum(p['totalBeds'] for p in values)
    return {'totalProperties':len(rows),'activeProperties':sum(p.status=='ACTIVE' for p in rows),
        'totalRooms':sum(p['totalRooms'] for p in values),'occupancyPercent':round(sum(p['occupiedBeds'] for p in values)/beds*100) if beds else 0,
        'cities':sorted({p.city for p in rows})}


@router.get('/properties/owners')
def owners(db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_CREATE'))):
    stmt = select(User).where(User.role_id=='OWNER',User.is_active.is_(True))
    if user.role_id != 'SUPER_ADMIN':
        stmt = stmt.where(User.id == user.id)
    return [{'id':str(u.id),'name':u.full_name} for u in db.scalars(stmt.order_by(User.full_name))]


@router.post('/properties',status_code=201,dependencies=[Depends(require_trusted_origin)])
def create(payload: PropertyWrite,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_CREATE'))):
    return service.output(service.write(db,payload,user))


@router.get('/properties/{property_id}')
def detail(property_id: str,db: Session=Depends(get_db),user=Depends(get_active_user)):
    return service.output(service.get(db,property_id,user),service.occupancy_counts(db))


@router.patch('/properties/{property_id}',dependencies=[Depends(require_trusted_origin)])
def edit(property_id: str,payload: PropertyWrite,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    return service.output(service.write(db,payload,user,service.get(db,property_id,user,True)),service.occupancy_counts(db))


@router.patch('/properties/{property_id}/status',dependencies=[Depends(require_trusted_origin)])
def status(property_id: str,payload: StatusInput,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    item = service.get(db,property_id,user,True)
    item.status = payload.status
    item.archived_at = None
    db.commit()
    return service.output(item,service.occupancy_counts(db))


@router.post('/properties/{property_id}/archive',dependencies=[Depends(require_trusted_origin)])
def archive(property_id: str,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    from app.core.security import now
    item = service.get(db,property_id,user,True)
    item.status, item.archived_at = 'ARCHIVED', now()
    db.commit()
    return service.output(item)


@router.get('/public/properties')
def public_list(db: Session=Depends(get_db),query: str=Query('',max_length=120),city: str='',type: str='',offset: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),
                sortBy: Literal['name','city','type','status','createdAt']='name',sortDirection: Literal['asc','desc']='asc'):
    return page(db,None,query,city,'',type,None,offset,limit,sortBy,sortDirection,True)


@router.get('/public/properties/{property_id}')
def public_detail(property_id: str,db: Session=Depends(get_db)):
    return service.output(service.get(db,property_id),public=True)


@router.get('/properties/{property_id}/images')
def gallery(property_id: str,db: Session=Depends(get_db),user=Depends(get_active_user)):
    return [service.image_output(i) for i in service.get(db,property_id,user).images]


@router.post('/properties/{property_id}/images',dependencies=[Depends(require_trusted_origin)])
async def upload(property_id: str,request: Request,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    # Authorize before multipart parsing; outer ASGI middleware bounds the entire body.
    from app.core.rate_limit import limiter
    limiter.auth(request, "property-image-upload", str(user.id))
    service.get(db,property_id,user)
    async with request.form(max_files=get_settings().property_max_images,max_fields=0,max_part_size=images.MAX_IMAGE_SIZE_BYTES) as form:
        files = form.getlist('files')
        if not files or any(not isinstance(f,UploadFile) for f in files) or any(k!='files' for k in form):
            raise HTTPException(422,'Select image files to upload.')
        prepared = []
        for file in files:
            content = await images.read_upload(file)
            result = await run_in_threadpool(images.process,content,file.filename,file.content_type)
            prepared.append((*result,len(content)))
        # Serialize concurrent uploads/deletes and re-read the gallery under the lock.
        item = service.get(db,property_id,user,True)
        images.persist_batch(db,item,prepared)
        return [service.image_output(i) for i in item.images]


@router.delete('/properties/{property_id}/images/{image_id}',dependencies=[Depends(require_trusted_origin)])
def remove(property_id: str,image_id: UUID,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    item = service.get(db,property_id,user,True)
    image = next((i for i in item.images if i.id==image_id),None)
    if not image:
        raise HTTPException(404,'Image not found.')
    keys = [v['key'] for v in image.variants.values()]
    item.images.remove(image)
    if image.is_cover and item.images:
        item.images[0].is_cover = True
    db.commit()
    # DB first: failed commits cannot leave broken image references.
    for key in keys:
        storage.delete(key)
    return {'message':'Image deleted.'}


@router.patch('/properties/{property_id}/cover-image',dependencies=[Depends(require_trusted_origin)])
def cover(property_id: str,payload: CoverInput,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    item = service.get(db,property_id,user,True)
    if payload.imageId not in {i.id for i in item.images}:
        raise HTTPException(422,'Image does not belong to this property.')
    for image in item.images:
        image.is_cover = image.id == payload.imageId
    db.commit()
    return [service.image_output(i) for i in item.images]


@router.patch('/properties/{property_id}/images/order',dependencies=[Depends(require_trusted_origin)])
def order(property_id: str,payload: OrderInput,db: Session=Depends(get_db),user=Depends(require_permission('PROPERTY_EDIT'))):
    item = service.get(db,property_id,user,True)
    if len(payload.imageIds)!=len(item.images) or set(payload.imageIds)!={i.id for i in item.images}:
        raise HTTPException(422,'Include every image exactly once.')
    for image in item.images:
        image.display_order = payload.imageIds.index(image.id)
    db.commit()
    return [service.image_output(i) for i in sorted(item.images,key=lambda i:i.display_order)]


@router.get('/property-images/{image_id}/{variant}')
def image_file(image_id: UUID,variant: Literal['thumbnail','medium','large'],request: Request,db: Session=Depends(get_db)):
    image = db.get(PropertyImage,image_id)
    if not image:
        raise HTTPException(404,'Image not found.')
    item = db.get(Property,image.property_id)
    public = db.scalar(service.public_scope(select(Property.id)).where(Property.id==item.id))
    if not public:
        # Private gallery URLs use a short-lived HMAC capability, never a login token.
        import hmac, time
        expiry = request.query_params.get('expires','')
        signature = request.query_params.get('signature','')
        expected = service.image_signature(image_id,expiry)
        if not expiry.isdigit() or int(expiry)<time.time() or not hmac.compare_digest(signature,expected):
            raise HTTPException(404,'Image not found.')
    path = storage.path(image.variants[variant]['key'])
    if not path.is_file():
        raise HTTPException(404,'Image unavailable.')
    return FileResponse(path,media_type='image/webp',headers={'Cache-Control':'public, max-age=86400, immutable' if public else 'private, max-age=300','X-Content-Type-Options':'nosniff'})
