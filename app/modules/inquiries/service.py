from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select, text

from app.core.security import digest, now
from app.modules.auth.dependencies import ensure_property_access, trusted_property_ids
from app.modules.inquiries.models import Inquiry, InquiryStatusEvent
from app.modules.inventory import service as inventory

TRANSITIONS = {
    'SUBMITTED': {'CONTACTED', 'REJECTED', 'WITHDRAWN'},
    'CONTACTED': {'SHORTLISTED', 'REJECTED', 'WITHDRAWN'},
    'SHORTLISTED': {'APPROVED', 'REJECTED', 'WITHDRAWN'},
    'APPROVED': {'REJECTED', 'WITHDRAWN'},
}


def serial_lock(db, key):
    # Transaction-scoped locks cover keys for which a row does not yet exist.
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(digest(key)[:15], 16)})


def ownership(user):
    from app.modules.tenants.models import Tenant
    # A server-linked tenant profile is controlled identity evidence; an arbitrary
    # submitted email or a privileged account's matching email is never enough.
    linked = exists(select(Tenant.id).where(Tenant.user_id == user.id, Tenant.email == Inquiry.email))
    return or_(Inquiry.user_id == user.id, and_(Inquiry.user_id.is_(None), Inquiry.email == user.email, linked))


def owns(db, item, user):
    return bool(db.scalar(select(Inquiry.id).where(Inquiry.id == item.id, ownership(user))))


def scoped(query, model, user):
    if user.role_id == 'SUPER_ADMIN':
        return query
    if user.role_id == 'TENANT':
        return query.where(ownership(user) if model is Inquiry else model.user_id == user.id)
    return query.where(model.property_id.in_(trusted_property_ids(user)))


def access(item, user, db=None):
    if user.role_id == 'TENANT':
        if item.user_id != user.id and not (db is not None and isinstance(item, Inquiry) and owns(db, item, user)):
            raise HTTPException(404, 'Record not found.')
    elif item.property_id:
        ensure_property_access(user, item.property_id)
    elif user.role_id != 'SUPER_ADMIN':
        raise HTTPException(403, 'Only the Super Admin can manage unscoped records.')


def get(db, inquiry_id, user, lock=False):
    query = select(Inquiry).where(Inquiry.id == inquiry_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query.execution_options(populate_existing=True))
    if not item:
        raise HTTPException(404, 'Inquiry not found.')
    access(item, user, db)
    return item


def event(db, item, status, user=None, note=None):
    previous = item.status
    item.status, item.updated_at = status, now()
    db.add(InquiryStatusEvent(inquiry_id=item.id, previous=previous, status=status,
        actor_user_id=user.id if user else None, actor_name=user.full_name if user else 'Guest', note=note))


def output(item, self_service=False):
    data = dict(item.data)
    return data | {'id': str(item.id), 'kind': item.kind, 'email': item.email,
        'userId': str(item.user_id) if item.user_id else None,
        'propertyId': item.property_id or '', 'roomId': item.room_id or '', 'bedId': item.bed_id,
        'status': item.status, 'tenantId': str(item.tenant_id) if item.tenant_id else None,
        'createdAt': item.created_at.isoformat(), 'updatedAt': item.updated_at.isoformat(),
        'statusHistory': [{'id': str(e.id), 'from': e.previous, 'to': e.status,
            'actor': 'StayOps' if self_service and e.actor_user_id != item.user_id else e.actor_name,
            'note': None if self_service else e.note, 'occurredAt': e.occurred_at.isoformat()} for e in item.events]}


def create(db, payload, user=None, key=None, general=False):
    fingerprint = digest(payload.model_dump_json() + ':' + (str(user.id) if user else 'guest'))
    if key:
        key = digest('inquiry:' + key)
        serial_lock(db, key)
        existing = db.scalar(select(Inquiry).where(Inquiry.idempotency_key == key))
        if existing:
            if existing.request_digest != fingerprint:
                raise HTTPException(409, 'Idempotency key has already been used for another request.')
            return existing
    data = payload.model_dump(mode='json')
    email = str(payload.email)
    if user and email != user.email:
        raise HTTPException(422, 'Use your account email for authenticated inquiries.')
    if general:
        first, _, last = payload.name.partition(' ')
        data = {'firstName': first, 'lastName': last, 'fullName': payload.name, 'phone': '',
            'propertyName': 'General inquiry', 'roomNumber': '', 'bedNumber': None,
            'listingTitle': f'General {payload.inquiryType} inquiry', 'sharingType': None,
            'monthlyRent': 0, 'preferredMoveIn': None, 'message': ''}
        kind, property_id, room_id, bed_id = payload.inquiryType, None, None, None
    else:
        room, bed = inventory.selection(payload.roomId, payload.bedId, payload.propertyId, db=db)
        if user is None or user.role_id == 'TENANT':
            from app.modules.properties.service import get as public_property
            public_property(db,room['propertyId'])
        inventory.available(db, room, bed)
        data |= inventory.snapshot(room, bed, db)
        data['fullName'] = f'{payload.firstName} {payload.lastName}'
        kind, property_id, room_id, bed_id = 'ROOM', room['propertyId'], room['id'], payload.bedId
        serial_lock(db, f'open:{email}:{room_id}:{bed_id}')
        duplicate = db.scalar(select(Inquiry.id).where(Inquiry.email == email, Inquiry.room_id == room_id,
            Inquiry.bed_id == bed_id, Inquiry.status.in_(TRANSITIONS)))
        if duplicate:
            raise HTTPException(409, 'You already have an open inquiry for this selection.')
    item = Inquiry(email=email, user_id=user.id if user else None, kind=kind, property_id=property_id,
        room_id=room_id, bed_id=bed_id, data=data, status='SUBMITTED', idempotency_key=key, request_digest=fingerprint)
    db.add(item)
    db.flush()
    db.add(InquiryStatusEvent(inquiry_id=item.id, previous=None, status='SUBMITTED',
        actor_user_id=user.id if user else None, actor_name=user.full_name if user else 'Guest'))
    db.commit()
    db.refresh(item)
    return item


def change(db, item, status, user, note=None):
    if status not in TRANSITIONS.get(item.status, set()):
        raise HTTPException(409, 'This inquiry status transition is not allowed.')
    event(db, item, status, user, note)
    db.commit()
    db.refresh(item)
    return item


def convert(db, item, user, allocate):
    from app.modules.tenants.models import Tenant
    from app.modules.tenants import service as tenants
    from app.modules.tenants.schemas import TenantWrite
    if item.status != 'APPROVED' or item.tenant_id:
        raise HTTPException(409, 'Only an approved, unconverted inquiry can be converted.')
    if item.kind != 'ROOM':
        raise HTTPException(422, 'General leads must be onboarded through Tenant Management.')
    serial_lock(db, 'tenant-email:' + item.email)
    if db.scalar(select(Tenant.id).where(Tenant.email == item.email)):
        raise HTTPException(409, 'A tenant profile already exists for this email.')
    data = item.data
    values = TenantWrite(firstName=data['firstName'], lastName=data['lastName'], email=item.email,
        phone=data['phone'], propertyId=item.property_id, roomId=item.room_id if allocate else None,
        bedId=item.bed_id if allocate else None, moveInDate=data.get('preferredMoveIn'), source='INQUIRY')
    if allocate and not item.bed_id:
        raise HTTPException(422, 'Select a specific bed before allocation.')
    tenant = tenants.create(db, values, user, commit=False, inquiry=item)
    item.tenant_id = tenant.id
    event(db, item, 'CONVERTED', user, 'Converted with allocation.' if allocate else 'Converted without allocation.')
    db.commit()
    db.refresh(item)
    return {'inquiry': output(item), 'tenant': tenants.output(tenant)}
