from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import object_session
from sqlalchemy import delete, exists, not_, select, text, update

from app.core.config import get_settings
from app.core.security import digest, now, password_hasher, random_token
from app.modules.auth import repository, service as auth_service
from app.modules.auth.dependencies import ensure_property_access, trusted_property_ids
from app.modules.auth.models import PasswordResetToken, PropertyAssignment, User
from app.modules.inventory import service as inventory
from app.modules.staff.models import StaffLinkInvitation, StaffProfile
from app.modules.staff.schemas import Assignments, StaffFields
from app.modules.tenants.models import Tenant
from app.services import email_service

STAFF_ROLES = {'WARDEN', 'ACCOUNTANT', 'SECURITY', 'MAINTENANCE'}
PROFILE_PATHS = {role: '/' + role.lower() + '/profile' for role in STAFF_ROLES}


def validate_assignments(user, properties):
    for property_id in properties:
        inventory.property_name(property_id, db=object_session(user))
        ensure_property_access(user, property_id)


def scope_query(stmt, user):
    if user.role_id == 'SUPER_ADMIN':
        return stmt
    allowed = trusted_property_ids(user)
    assignments = select(PropertyAssignment.property_id).where(PropertyAssignment.user_id == StaffProfile.user_id)
    # A shared employee's account cannot be managed from just one of their sites.
    return stmt.where(StaffProfile.link_confirmed.is_(True), exists(assignments),
        not_(exists(assignments.where(PropertyAssignment.property_id.not_in(allowed)))))


def check_access(item, user):
    if item.user.role_id not in STAFF_ROLES:
        raise HTTPException(409, 'The linked account no longer has a compatible staff role.')
    if user.role_id == 'SUPER_ADMIN':
        return
    if not item.link_confirmed or not item.user.property_assignments:
        raise HTTPException(403, 'You do not have access to this staff member.')
    for assignment in item.user.property_assignments:
        ensure_property_access(user, assignment.property_id)


def get(db, staff_id, user, lock=False):
    item = db.get(StaffProfile, staff_id)
    if not item:
        raise HTTPException(404, 'Staff member not found.')
    if lock:
        # Same lock order as login/reset/refresh: account before dependent records.
        repository.lock_user(db, item.user_id)
        item = db.scalar(select(StaffProfile).where(StaffProfile.id == staff_id).with_for_update().execution_options(populate_existing=True))
    check_access(item, user)
    return item


def output(item):
    account = item.user
    first, _, last = account.full_name.partition(' ')
    properties = sorted(a.property_id for a in account.property_assignments)
    if not item.link_confirmed:
        properties = item.invitation.proposed_property_ids if item.invitation else []
    return {'id': str(item.id), 'userId': str(account.id), 'employeeCode': item.employee_code,
        'firstName': first, 'lastName': last, 'fullName': account.full_name, 'email': account.email,
        'role': account.role_id, 'phone': item.phone, 'department': item.department, 'designation': item.designation,
        'propertyIds': properties, 'propertyNames': [inventory.property_name(p, db=object_session(item)) for p in properties],
        'status': item.status, 'joiningDate': item.joining_date.isoformat(), 'profileImage': account.avatar,
        'address': item.address, 'emergencyContact': item.emergency_contact,
        'accountLinkPending': not item.link_confirmed, 'accountEnabled': account.is_active,
        'createdAt': item.created_at.isoformat(), 'updatedAt': item.updated_at.isoformat()}


def replace_assignments(db, account, properties):
    if sorted(a.property_id for a in account.property_assignments) == sorted(properties):
        return
    db.execute(delete(PropertyAssignment).where(PropertyAssignment.user_id == account.id))
    db.flush()
    db.expire(account, ['property_assignments'])
    account.property_assignments = [PropertyAssignment(property_id=p) for p in properties]
    # New JWTs resolve current scopes; revoke existing sessions for removals too.
    repository.revoke_sessions(db, account.id)


def apply_fields(item, values, identity=True):
    if identity:
        item.user.full_name = f'{values.firstName} {values.lastName}'.strip()
        item.user.avatar = values.profileImage or None
    item.phone, item.department, item.designation = values.phone, values.department, values.designation
    item.joining_date = values.joiningDate
    item.address = {key: getattr(values, key) for key in ('addressLine1', 'addressLine2', 'city', 'state', 'postalCode', 'country')}
    item.emergency_contact = {'name': values.emergencyName, 'relationship': values.emergencyRelationship, 'phone': values.emergencyPhone}
    item.updated_at = now()


def change_status(db, item, status):
    if not item.link_confirmed:
        raise HTTPException(409, 'The existing account must accept its link invitation first.')
    item.status, item.updated_at = status, now()
    if status == 'INACTIVE':
        item.user.is_active = False
        auth_service.invalidate_credentials(db, item.user)
    elif status == 'ACTIVE':
        item.user.is_active = True
    # ON_LEAVE preserves login and does not revoke active sessions.


def send_invitation(db, item, properties=None):
    raw = random_token()
    settings = get_settings()
    expiry = now() + timedelta(minutes=settings.reset_token_expire_minutes)
    if not item.link_confirmed:
        invitation = item.invitation
        if invitation is None:
            invitation = StaffLinkInvitation(staff_id=item.id, proposed_property_ids=properties, expected_role=item.user.role_id)
            db.add(invitation)
        invitation.token_digest, invitation.expires_at, invitation.used_at = digest(raw), expiry, None
        link = settings.frontend_url.rstrip('/') + PROFILE_PATHS[item.user.role_id] + '#staff-link=' + quote(raw, safe='')
        sender = email_service.send_staff_link_email
    else:
        db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == item.user_id,
            PasswordResetToken.used_at.is_(None)).values(used_at=now()))
        db.add(PasswordResetToken(user_id=item.user_id, token_digest=digest(raw), expires_at=expiry))
        link = settings.frontend_url.rstrip('/') + '/reset-password#token=' + quote(raw, safe='')
        sender = email_service.send_staff_setup_email
    db.flush()
    try:
        sender(item.user.email, link)
    except Exception:
        db.rollback()
        raise HTTPException(503, 'Staff invitation delivery failed. No changes were saved; please retry.') from None


def create(db, values, admin):
    # Serialize the absent-account case; unique indexes also guard other writers.
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(digest('staff-email:' + str(values.email))[:15], 16)})
    validate_assignments(admin, values.propertyIds)
    account = repository.user_by_email(db, str(values.email), lock=True)
    if account:
        if account.role_id != values.role or account.role_id not in STAFF_ROLES or not account.is_active:
            raise HTTPException(409, 'This email belongs to an incompatible or disabled account.')
        if db.scalar(select(StaffProfile.id).where(StaffProfile.user_id == account.id)) or db.scalar(select(Tenant.id).where(Tenant.user_id == account.id)):
            raise HTTPException(409, 'This account is already linked to a profile.')
        if values.existingUserId != account.id:
            raise HTTPException(409, 'An account already exists. Explicitly select it and request mailbox confirmation.')
    elif values.existingUserId:
        raise HTTPException(409, 'The selected account does not match the email.')
    new_account = account is None
    if new_account:
        account = User(full_name=f'{values.firstName} {values.lastName}', email=str(values.email), role_id=values.role,
            password_hash=password_hasher.hash(random_token()), is_active=values.status != 'INACTIVE', avatar=values.profileImage or None)
        db.add(account)
        db.flush()
        replace_assignments(db, account, values.propertyIds)
    item = StaffProfile(user=account, status=values.status, link_confirmed=new_account)
    apply_fields(item, values, identity=new_account)
    db.add(item)
    db.flush()
    send_invitation(db, item, values.propertyIds)
    db.commit()
    db.refresh(item)
    return item


def candidate(db, email):
    account = repository.user_by_email(db, email)
    if not account:
        raise HTTPException(404, 'No existing account was found. Create a new staff account.')
    if account.role_id not in STAFF_ROLES or not account.is_active or db.scalar(select(StaffProfile.id).where(StaffProfile.user_id == account.id)) or db.scalar(select(Tenant.id).where(Tenant.user_id == account.id)):
        raise HTTPException(409, 'This account cannot be linked to a new staff profile.')
    return {'id': str(account.id), 'name': account.full_name, 'email': account.email, 'role': account.role_id}


def update_profile(db, item, payload, admin):
    if not item.link_confirmed:
        raise HTTPException(409, 'Wait for account-link acceptance before editing this profile.')
    current = output(item)
    raw = {key: current[key] for key in ('firstName', 'lastName', 'phone', 'department', 'designation', 'joiningDate')}
    raw |= current['address'] | {'profileImage': current['profileImage'] or '',
        'emergencyName': current['emergencyContact']['name'], 'emergencyRelationship': current['emergencyContact']['relationship'],
        'emergencyPhone': current['emergencyContact']['phone']}
    patch = payload.model_dump(exclude_unset=True, mode='json')
    properties, status = patch.pop('propertyIds', current['propertyIds']), patch.pop('status', item.status)
    try:
        values = StaffFields.model_validate(raw | patch)
        assignments = Assignments(propertyIds=properties)
        if status is None:
            raise ValueError('Status required')
    except (ValidationError, ValueError):
        raise HTTPException(422, 'Invalid staff details. Check required fields, contact information and dates.') from None
    validate_assignments(admin, assignments.propertyIds)
    apply_fields(item, values)
    if sorted(properties) != sorted(current['propertyIds']):
        replace_assignments(db, item.user, properties)
    if status != item.status:
        change_status(db, item, status)
    db.commit()
    db.refresh(item)
    return item


def accept(db, user, token):
    invitation = db.scalar(select(StaffLinkInvitation).where(StaffLinkInvitation.token_digest == digest(token)))
    if not invitation:
        raise HTTPException(400, 'Staff invitation is invalid or expired.')
    account = repository.lock_user(db, user.id)
    item = db.scalar(select(StaffProfile).where(StaffProfile.id == invitation.staff_id).with_for_update())
    db.refresh(invitation)
    if not item or item.user_id != user.id or account.role_id not in STAFF_ROLES or account.role_id != invitation.expected_role or invitation.used_at or invitation.expires_at <= now() or item.link_confirmed:
        raise HTTPException(400, 'Staff invitation is invalid or expired.')
    validate_assignments_for_accept(invitation.proposed_property_ids, db)
    replace_assignments(db, account, invitation.proposed_property_ids)
    item.link_confirmed, invitation.used_at = True, now()
    change_status(db, item, item.status)
    db.commit()
    return {'message': 'Staff account linked. Sign in again to load your current assignments.'}


def validate_assignments_for_accept(properties, db):
    for property_id in properties:
        inventory.property_name(property_id, db=db)
