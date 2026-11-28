from fastapi import HTTPException
from sqlalchemy import select, func, delete
from app.core.config import get_settings
from app.core.security import now
from app.modules.auth.models import User, PropertyAssignment
from app.modules.properties.models import Property
from sqlalchemy.orm import object_session
from app.modules.tenants.models import Occupancy


def public_scope(stmt):
    stmt = stmt.where(Property.status == 'ACTIVE', Property.is_public.is_(True))
    if get_settings().app_env == 'production':
        stmt = stmt.where(Property.is_demo.is_(False))
    return stmt


def scoped(stmt, user):
    if user.role_id == 'SUPER_ADMIN':
        return stmt
    if user.role_id == 'OWNER':
        return stmt.where(Property.owner_user_id == user.id)
    if user.role_id in {'WARDEN','ACCOUNTANT','SECURITY','MAINTENANCE'}:
        return stmt.where(Property.id.in_([a.property_id for a in user.property_assignments]))
    raise HTTPException(403, 'Property management is not available for this role.')


def get(db, key, user=None, lock=False):
    stmt = select(Property).where(Property.id == key)
    stmt = scoped(stmt, user) if user else public_scope(stmt)
    if lock:
        stmt = stmt.with_for_update()
    item = db.scalar(stmt.execution_options(populate_existing=True))
    if not item:
        raise HTTPException(404, 'Property not found or access denied.')
    return item


def write(db, payload, user, item=None):
    if user.role_id not in {'SUPER_ADMIN','OWNER'}:
        raise HTTPException(403, 'Only property owners and the Super Admin can edit properties.')
    owner_id = payload.ownerUserId
    if user.role_id == 'OWNER':
        if owner_id and owner_id != user.id:
            raise HTTPException(403, 'Owners cannot assign another owner.')
        owner_id = user.id
    if owner_id:
        owner = db.get(User, owner_id)
        if not owner or owner.role_id != 'OWNER' or not owner.is_active:
            raise HTTPException(422, 'Select an active Owner account.')
    if payload.isDemo and get_settings().app_env == 'production':
        raise HTTPException(422, 'Demo properties are disabled in production.')
    if item and item.is_demo and not payload.isDemo:
        raise HTTPException(422, 'Demo records cannot be converted to real listings.')
    # Serialize duplicate creates even before a property row exists.
    from app.modules.inquiries.service import serial_lock
    serial_lock(db, 'property:' + payload.name.casefold() + ':' + payload.address.casefold())
    duplicate = select(Property.id).where(func.lower(Property.name) == payload.name.lower(), func.lower(Property.address) == payload.address.lower())
    if item:
        duplicate = duplicate.where(Property.id != item.id)
    if db.scalar(duplicate):
        raise HTTPException(409, 'A property with this name and address already exists.')
    old_owner = item.owner_user_id if item else None
    item = item or Property()
    mapping = {'postalCode':'postal_code','contactNumber':'contact_number','ownerUserId':'owner_user_id','isPublic':'is_public','isDemo':'is_demo'}
    for key,value in payload.model_dump().items():
        setattr(item,mapping.get(key,key),value)
    item.owner_user_id = owner_id
    item.archived_at = now() if item.status == 'ARCHIVED' else None
    db.add(item)
    db.flush()
    if old_owner != owner_id:
        if old_owner:
            db.execute(delete(PropertyAssignment).where(PropertyAssignment.user_id == old_owner, PropertyAssignment.property_id == item.id))
        if owner_id and not db.get(PropertyAssignment, (owner_id,item.id)):
            db.add(PropertyAssignment(user_id=owner_id,property_id=item.id))
    db.commit()
    return item


def image_output(image):
    import time
    base = f'/api/v1/property-images/{image.id}'
    expiry = str((int(time.time()) // 1800 + 2) * 1800)
    suffix = '?expires=' + expiry + '&signature=' + image_signature(image.id, expiry)
    return {'id':str(image.id),'filename':image.original_filename_display,'width':image.image_width,'height':image.image_height,
        'originalSize':image.original_file_size,'isCover':image.is_cover,'displayOrder':image.display_order,
        **{name:base + '/' + name + suffix for name in ('thumbnail','medium','large')},
        'variants':{name:{k:v for k,v in data.items() if k != 'key'} for name,data in image.variants.items()}}


def occupancy_counts(db):
    from app.modules.inventory.management import hierarchy
    return {'hierarchy':hierarchy(db,list(db.scalars(select(Property.id)))), 'revenues':monthly_revenues(db)}


def monthly_revenues(db, property_ids=None):
    from app.modules.finance.business_date import business_today
    from app.modules.finance.models import Payment
    stmt=select(Payment.property_id,func.sum(Payment.amount)).where(Payment.status=='SUCCESS', Payment.source=='MANUAL',
        Payment.payment_date >= business_today().replace(day=1)).group_by(Payment.property_id)
    if property_ids is not None:
        stmt=stmt.where(Payment.property_id.in_(property_ids))
    return {key:float(amount) for key,amount in db.execute(stmt)}



def output(item, occupied=None, public=False, detail=True, user=None):
    from app.modules.inventory.management import hierarchy
    db=object_session(item)
    buildings=(occupied or {}).get('hierarchy',{}).get(item.id)
    if buildings is None:
        buildings=hierarchy(db,[item.id]).get(item.id,[]) if db else []
    floors=[f for b in buildings for f in b['floors']]
    beds=sum(f['bedCount'] for f in floors)
    count=sum(f['occupiedBeds'] for f in floors)
    gallery = [image_output(i) for i in item.images]
    cover = next((i for i in gallery if i['isCover']), gallery[0] if gallery else None)
    result = dict(id=item.id,name=item.name,type=item.type,address=item.address,locality=item.locality,city=item.city,
        state=item.state,postalCode=item.postal_code,country=item.country,description=item.description,amenities=item.amenities,
        status=item.status,latitude=item.latitude,longitude=item.longitude,isDemo=item.is_demo,
        images=gallery if detail else [],coverImage=cover)
    if not public:
        can_finance=user is not None and user.role_id!='TENANT' and 'PAYMENT_VIEW' in {p.id for p in user.role.permissions}
        revenue=(occupied or {}).get('revenues')
        if revenue is None and can_finance:
            revenue=monthly_revenues(db,[item.id])
        result.update(ownerUserId=str(item.owner_user_id) if item.owner_user_id else None,isPublic=item.is_public,
            contactNumber=item.contact_number,email=item.email,monthlyRevenue=(revenue or {}).get(item.id,0) if can_finance else None,recentActivity=[],
            buildings=buildings,
            totalBuildings=len(buildings),totalFloors=len(floors),
            totalRooms=sum(f['roomCount'] for f in floors),totalBeds=beds,occupiedBeds=count,availableBeds=sum(f['availableBeds'] for f in floors),
            occupancyPercent=round(count/beds*100) if beds else 0)
    return result


def image_signature(image_id, expiry):
    import hmac, hashlib
    return hmac.new(get_settings().jwt_secret_key.get_secret_value().encode(), f'property-image:{image_id}:{expiry}'.encode(), hashlib.sha256).hexdigest()
