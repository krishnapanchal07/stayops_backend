from datetime import date, timedelta
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.security import digest, now, password_hasher, random_token
from app.modules.auth.dependencies import ensure_property_access
from app.modules.auth.models import PasswordResetToken, User
from app.modules.inquiries.service import access, serial_lock
from app.modules.inventory import service as inventory
from app.modules.tenants.models import Occupancy, Tenant, TenantActivity, TenantInvitation
from app.modules.tenants.schemas import TenantWrite
from app.services import email_service

TRANSITIONS = {
    'PROSPECT': {'ONBOARDING', 'INACTIVE'},
    'ONBOARDING': {'ACTIVE', 'INACTIVE', 'CHECKED_OUT'},
    'ACTIVE': {'NOTICE_PERIOD', 'INACTIVE', 'CHECKED_OUT'},
    'NOTICE_PERIOD': {'ACTIVE', 'INACTIVE', 'CHECKED_OUT'},
    'INACTIVE': {'ONBOARDING', 'ACTIVE', 'CHECKED_OUT'},
    'CHECKED_OUT': {'ONBOARDING'},
}
ALLOCATION_KEYS = ('buildingId', 'buildingName', 'floorId', 'floorName', 'roomNumber', 'bedLabel', 'roomRent')


def permission(user, code):
    if code not in {p.id for p in user.role.permissions}:
        raise HTTPException(403, 'You do not have permission for this operation.')


def get(db, tenant_id, user, lock=False):
    query = select(Tenant).where(Tenant.id == tenant_id)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query.execution_options(populate_existing=True))
    if not item:
        raise HTTPException(404, 'Tenant not found.')
    access(item, user)
    return item


def me(db, user):
    if user.role_id != 'TENANT':
        raise HTTPException(403, 'Tenant account required.')
    return db.scalar(select(Tenant).where(Tenant.user_id == user.id))


def activity(db, item, user, action, description):
    db.add(TenantActivity(tenant_id=item.id, actor_user_id=user.id, actor_name=user.full_name,
        action=action, description=description))
    item.updated_at = now()


def output(item, personal=False):
    if not item:
        return None
    data = item.data
    address = {key: data.get(key, '') for key in ('addressLine1', 'addressLine2', 'city', 'state', 'postalCode', 'country')}
    emergency = {key: data.get('emergency' + key[0].upper() + key[1:], '') for key in ('name', 'relationship', 'phone', 'email', 'address')}
    result = {key: data.get(key) for key in ('firstName', 'lastName', 'dateOfBirth', 'gender', 'profileImage',
        'propertyName', *ALLOCATION_KEYS, 'securityDeposit', 'moveInDate', 'expectedMoveOutDate', 'actualMoveOutDate')}
    result |= {'id': str(item.id), 'tenantCode': item.tenant_code, 'email': item.email,
        'phone': data['phone'], 'fullName': f"{data['firstName']} {data['lastName']}",
        'propertyId': item.property_id, 'roomId': item.room_id, 'bedId': item.bed_id,
        'status': item.status, 'source': item.source, 'address': address, 'emergencyContact': emergency,
        'inquiryId': str(item.inquiry_id) if item.inquiry_id else None,
        'createdAt': item.created_at.isoformat(), 'updatedAt': item.updated_at.isoformat()}
    if not personal:
        result |= {'internalNotes': item.internal_notes, 'userId': str(item.user_id) if item.user_id else None}
    return result


def release(db, item):
    db.execute(delete(Occupancy).where(Occupancy.tenant_id == item.id))
    item.room_id = item.bed_id = None
    item.data = dict(item.data) | {key: None for key in ALLOCATION_KEYS}


def allocate(db, item, room_id, bed_id, user, move_in=None):
    permission(user, 'ROOM_ALLOCATE')
    room, bed = inventory.selection(room_id, bed_id, db=db)
    ensure_property_access(user, room['propertyId'])
    inventory.available(db, room, bed, item.id)
    if not bed:
        raise HTTPException(422, 'A bed is required for allocation.')
    if item.status in {'CHECKED_OUT', 'INACTIVE'}:
        raise HTTPException(409, 'Reopen onboarding before allocating a bed.')
    # Tenant row is locked by callers. Unique bed PK serializes competing tenants.
    release(db, item)
    db.flush()
    db.add(Occupancy(bed_id=bed['id'], tenant_id=item.id, room_id=room['id'], property_id=room['propertyId']))
    snap = inventory.snapshot(room, bed, db)
    item.property_id, item.room_id, item.bed_id = room['propertyId'], room['id'], bed['id']
    item.data = dict(item.data) | {key: snap[key] for key in ('propertyName', *ALLOCATION_KEYS) if key != 'roomRent'} | {
        'roomRent': snap['monthlyRent'], 'securityDeposit': snap['securityDeposit'],
        'moveInDate': str(move_in or date.today())}
    activity(db, item, user, 'Room assigned', f"Assigned {snap['listingTitle']}.")
    db.flush()


def status(db, item, target, user, move_out=None):
    if target == item.status:
        return
    if target not in TRANSITIONS.get(item.status, set()):
        raise HTTPException(409, 'This tenant status transition is not allowed.')
    if target in {'ACTIVE', 'NOTICE_PERIOD'}:
        ledger = db.scalar(select(Occupancy).where(Occupancy.tenant_id == item.id))
        if not ledger or ledger.bed_id != item.bed_id or ledger.room_id != item.room_id:
            raise HTTPException(409, 'Allocate a room and bed before activation.')
    if target in {'CHECKED_OUT', 'INACTIVE'}:
        if move_out and item.data.get('moveInDate') and move_out < date.fromisoformat(item.data['moveInDate']):
            raise HTTPException(422, 'Move-out cannot be before move-in.')
        release(db, item)
        if target == 'CHECKED_OUT':
            item.data = dict(item.data) | {'actualMoveOutDate': str(move_out or date.today())}
    if item.status == 'CHECKED_OUT' and target == 'ONBOARDING':
        item.data = dict(item.data) | {'actualMoveOutDate': None}
    previous = item.status
    item.status = target
    activity(db, item, user, 'Status changed', f'{previous} → {target}')


def create(db, values, user, commit=True, inquiry=None):
    if values.source == 'INQUIRY' and not inquiry:
        raise HTTPException(422, 'Use inquiry conversion to create an inquiry-linked tenant.')
    if values.propertyId:
        name = inventory.property_name(values.propertyId, db)
        ensure_property_access(user, values.propertyId)
    elif user.role_id != 'SUPER_ADMIN':
        raise HTTPException(403, 'A scoped property is required.')
    else:
        name = None
    serial_lock(db, 'tenant-email:' + str(values.email))
    if db.scalar(select(Tenant.id).where(Tenant.email == str(values.email))):
        raise HTTPException(409, 'A tenant profile already exists for this email.')
    if bool(values.roomId) != bool(values.bedId):
        raise HTTPException(422, 'Select both room and bed, or neither.')
    data = values.model_dump(mode='json') | {'propertyName': name, 'actualMoveOutDate': None}
    for key in ALLOCATION_KEYS:
        data[key] = None
    item = Tenant(email=str(values.email), property_id=values.propertyId, status='ONBOARDING', source=values.source,
        inquiry_id=inquiry.id if inquiry else None, data=data, internal_notes=values.internalNotes)
    # An authenticated inquiry proves only the submitting user's identity.
    if inquiry and inquiry.user_id:
        account = db.get(User, inquiry.user_id)
        if account and account.role_id == 'TENANT' and account.email == item.email:
            item.user_id = account.id
    db.add(item)
    db.flush()
    activity(db, item, user, 'Tenant created', 'Tenant profile created.')
    if values.roomId:
        inventory.selection(values.roomId, values.bedId, values.propertyId, db=db)
        allocate(db, item, values.roomId, values.bedId, user, values.moveInDate)
    if values.status == 'PROSPECT':
        if item.bed_id:
            raise HTTPException(422, 'Prospects cannot hold allocations.')
        item.status = 'PROSPECT'
    elif values.status != 'ONBOARDING':
        status(db, item, values.status, user)
    if commit:
        db.commit()
    return item


def update(db, item, patch, user):
    raw = {key: item.data.get(key, field.default) for key, field in TenantWrite.model_fields.items()}
    raw |= {'email': item.email, 'status': item.status, 'source': item.source, 'propertyId': item.property_id,
        'roomId': item.room_id, 'bedId': item.bed_id, 'roomRent': item.data.get('roomRent') or 0,
        'internalNotes': item.internal_notes}
    raw |= patch.model_dump(exclude_unset=True, mode='json')
    try:
        values = TenantWrite.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, 'Invalid tenant fields. Check identity, contact and dates.') from None
    if values.email != item.email or values.source != item.source:
        raise HTTPException(422, 'Tenant email and source cannot be changed through profile editing.')
    if bool(values.roomId) != bool(values.bedId):
        raise HTTPException(422, 'Select both room and bed, or neither.')
    if values.propertyId:
        name = inventory.property_name(values.propertyId, db)
        ensure_property_access(user, values.propertyId)
    elif user.role_id != 'SUPER_ADMIN':
        raise HTTPException(403, 'A scoped property is required.')
    else:
        name = None
    allocation_changed = (values.roomId, values.bedId) != (item.room_id, item.bed_id)
    # Keep server-controlled allocation snapshots; form rent/location labels are advisory only.
    preserved = {key: item.data.get(key) for key in ALLOCATION_KEYS}
    item.data = dict(item.data) | values.model_dump(mode='json') | preserved | {'propertyName': name}
    item.property_id, item.internal_notes = values.propertyId, values.internalNotes
    if values.roomId:
        inventory.selection(values.roomId, values.bedId, values.propertyId, db=db)
        if allocation_changed:
            allocate(db, item, values.roomId, values.bedId, user, values.moveInDate)
    elif allocation_changed:
        permission(user, 'ROOM_ALLOCATE')
        release(db, item)
    if not allocation_changed and item.room_id:
        room, _ = inventory.selection(item.room_id, item.bed_id, values.propertyId, db=db)
        item.property_id = room['propertyId']
    status(db, item, values.status, user)
    if item.status in {'ACTIVE', 'NOTICE_PERIOD'} and not item.bed_id:
        raise HTTPException(409, 'Active tenants require an allocation.')
    activity(db, item, user, 'Profile updated', 'Tenant information updated.')
    db.commit()
    return item


def invite(db, item, admin):
    # Restricted provisioning uses the existing users and password-reset infrastructure.
    serial_lock(db, 'account-email:' + item.email)
    account = db.scalar(select(User).where(User.email == item.email).with_for_update())
    if account and (account.role_id != 'TENANT' or not account.is_active):
        raise HTTPException(409, 'This email belongs to an account that cannot be linked.')
    if account and db.scalar(select(Tenant.id).where(Tenant.user_id == account.id, Tenant.id != item.id)):
        raise HTTPException(409, 'This account is already linked to another tenant.')
    if account and item.user_id and item.user_id != account.id:
        raise HTTPException(409, 'Tenant is linked to another account.')
    if not account:
        account = User(full_name=f"{item.data['firstName']} {item.data['lastName']}", email=item.email,
            role_id='TENANT', password_hash=password_hasher.hash(random_token()))
        db.add(account)
        db.flush()
        # New account has an unguessable password; only mailbox possession enables sign-in.
        item.user_id = account.id
    raw = random_token()
    if item.user_id == account.id:
        # Reuse the existing single-use, expiring password setup/reset path.
        from sqlalchemy import update as sql_update
        db.execute(sql_update(PasswordResetToken).where(PasswordResetToken.user_id == account.id,
            PasswordResetToken.used_at.is_(None)).values(used_at=now()))
        db.add(PasswordResetToken(user_id=account.id, token_digest=digest(raw),
            expires_at=now() + timedelta(minutes=get_settings().reset_token_expire_minutes)))
        link = get_settings().frontend_url.rstrip('/') + '/reset-password#token=' + quote(raw, safe='')
        sender = email_service.send_reset_email
    else:
        db.execute(delete(TenantInvitation).where(TenantInvitation.tenant_id == item.id))
        db.add(TenantInvitation(tenant_id=item.id, email=item.email, token_digest=digest(raw),
            expires_at=now() + timedelta(minutes=20)))
        link = get_settings().frontend_url.rstrip('/') + '/tenant/profile#tenant-link=' + quote(raw, safe='')
        sender = email_service.send_tenant_link_email
    db.flush()
    try:
        sender(item.email, link)
    except Exception:
        db.rollback()
        raise HTTPException(503, 'Invitation delivery failed. Please retry.') from None
    activity(db, item, admin, 'Invitation sent', 'Account invitation delivered to the tenant mailbox.')
    db.commit()
    return {'message': 'Account invitation sent.'}


def accept(db, user, token):
    invitation = db.scalar(select(TenantInvitation).where(TenantInvitation.token_digest == digest(token)))
    if not invitation:
        raise HTTPException(400, 'Invitation is invalid or expired.')
    item = db.scalar(select(Tenant).where(Tenant.id == invitation.tenant_id).with_for_update())
    db.refresh(invitation)
    if invitation.used_at or invitation.expires_at <= now() or user.email != invitation.email or user.role_id != 'TENANT' or item.email != user.email:
        raise HTTPException(400, 'Invitation is invalid or expired.')
    if item.user_id and item.user_id != user.id:
        raise HTTPException(409, 'Tenant already linked.')
    item.user_id, invitation.used_at = user.id, now()
    # Mailbox proof now allows claiming prior guest room requests for this email.
    from app.modules.inquiries.models import Inquiry
    from sqlalchemy import update as sql_update
    db.execute(sql_update(Inquiry).where(Inquiry.email == user.email, Inquiry.user_id.is_(None)).values(user_id=user.id))
    activity(db, item, user, 'Account linked', 'Tenant accepted the mailbox invitation.')
    db.commit()
    return output(item, personal=True)
