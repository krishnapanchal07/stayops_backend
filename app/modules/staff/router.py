from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import EmailStr
from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, require_permission, require_trusted_origin
from app.modules.auth.models import PropertyAssignment, User
from app.modules.inventory import service as inventory
from app.modules.staff import service
from app.modules.staff.models import StaffLinkInvitation, StaffProfile
from app.modules.staff.schemas import AcceptInvitation, Assignments, Department, StaffCreate, StaffPatch, StaffRole, StaffStatus, StatusInput

router = APIRouter(prefix='/staff', tags=['Staff'])


def provisioner(user=Depends(require_permission('STAFF_CREATE'))):
    if user.role_id != 'SUPER_ADMIN':
        raise HTTPException(403, 'Only the Super Admin may provision or link login accounts.')
    return user


@router.get('')
def listing(db: Session = Depends(get_db), user=Depends(require_permission('STAFF_VIEW')),
            query: str = Query('', max_length=120), role: StaffRole | None = None,
            department: Department | None = None, status: StaffStatus | None = None,
            propertyId: str = Query('', max_length=64), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
            sortBy: Literal['fullName', 'email', 'employeeCode', 'role', 'department', 'status', 'createdAt'] = 'createdAt',
            sortDirection: Literal['asc', 'desc'] = 'desc'):
    stmt = service.scope_query(select(StaffProfile).join(User), user)
    if query.strip():
        needle = '%' + query.strip().replace('%', r'\%').replace('_', r'\_') + '%'
        stmt = stmt.where(or_(User.full_name.ilike(needle), User.email.ilike(needle),
            StaffProfile.employee_code.ilike(needle), StaffProfile.phone.ilike(needle)))
    for column, value in ((User.role_id, role), (StaffProfile.department, department), (StaffProfile.status, status)):
        if value:
            stmt = stmt.where(column == value)
    if propertyId:
        active = exists(select(PropertyAssignment.user_id).where(PropertyAssignment.user_id == StaffProfile.user_id,
            PropertyAssignment.property_id == propertyId))
        pending = exists(select(StaffLinkInvitation.id).where(StaffLinkInvitation.staff_id == StaffProfile.id,
            StaffLinkInvitation.proposed_property_ids.cast(JSONB).contains([propertyId])))
        stmt = stmt.where(or_(StaffProfile.link_confirmed.is_(True) & active, StaffProfile.link_confirmed.is_(False) & pending))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    column = {'fullName': User.full_name, 'email': User.email, 'employeeCode': StaffProfile.employee_code,
        'role': User.role_id, 'department': StaffProfile.department, 'status': StaffProfile.status, 'createdAt': StaffProfile.created_at}[sortBy]
    rows = db.scalars(stmt.order_by(column.asc() if sortDirection == 'asc' else column.desc(), StaffProfile.id).offset(offset).limit(limit))
    return {'items': [service.output(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


@router.post('', status_code=201, dependencies=[Depends(require_trusted_origin)])
def create(payload: StaffCreate, request: Request, db: Session = Depends(get_db), user=Depends(provisioner)):
    limiter.auth(request, 'staff-create', str(payload.email))
    return service.output(service.create(db, payload, user))


@router.get('/summary')
def summary(db: Session = Depends(get_db), user=Depends(require_permission('STAFF_VIEW'))):
    sub = service.scope_query(select(StaffProfile), user).subquery()
    counts = dict(db.execute(select(sub.c.status, func.count()).group_by(sub.c.status)).all())
    return {'total': sum(counts.values()), 'active': counts.get('ACTIVE', 0),
        'inactive': counts.get('INACTIVE', 0), 'onLeave': counts.get('ON_LEAVE', 0)}


@router.get('/properties')
def properties(db: Session = Depends(get_db), user=Depends(require_permission('STAFF_VIEW'))):
    from app.modules.properties.models import Property
    from app.modules.properties.service import scoped
    return [{'id':p.id,'name':p.name} for p in db.scalars(scoped(select(Property),user).where(Property.status != 'ARCHIVED').order_by(Property.name))]


@router.get('/account-candidate')
def candidate(email: EmailStr, db: Session = Depends(get_db), user=Depends(provisioner)):
    return service.candidate(db, str(email).strip().lower())


@router.post('/accept-invitation', dependencies=[Depends(require_trusted_origin)])
def accept(payload: AcceptInvitation, request: Request, db: Session = Depends(get_db), user=Depends(get_active_user)):
    limiter.auth(request, 'staff-accept', str(user.id))
    return service.accept(db, user, payload.token)


@router.get('/{staff_id}')
def detail(staff_id: UUID, db: Session = Depends(get_db), user=Depends(require_permission('STAFF_VIEW'))):
    return service.output(service.get(db, staff_id, user))


@router.patch('/{staff_id}', dependencies=[Depends(require_trusted_origin)])
def edit(staff_id: UUID, payload: StaffPatch, db: Session = Depends(get_db), user=Depends(require_permission('STAFF_EDIT'))):
    return service.output(service.update_profile(db, service.get(db, staff_id, user, True), payload, user))


@router.patch('/{staff_id}/status', dependencies=[Depends(require_trusted_origin)])
def status(staff_id: UUID, payload: StatusInput, db: Session = Depends(get_db), user=Depends(require_permission('STAFF_EDIT'))):
    item = service.get(db, staff_id, user, True)
    service.change_status(db, item, payload.status)
    db.commit()
    return service.output(item)


@router.patch('/{staff_id}/assignments', dependencies=[Depends(require_trusted_origin)])
def assignments(staff_id: UUID, payload: Assignments, db: Session = Depends(get_db), user=Depends(require_permission('STAFF_EDIT'))):
    item = service.get(db, staff_id, user, True)
    if not item.link_confirmed:
        raise HTTPException(409, 'The account must accept its link invitation first.')
    service.validate_assignments(user, payload.propertyIds)
    service.replace_assignments(db, item.user, payload.propertyIds)
    item.updated_at = service.now()
    db.commit()
    return service.output(item)


@router.post('/{staff_id}/resend-invite', dependencies=[Depends(require_trusted_origin)])
def resend(staff_id: UUID, request: Request, db: Session = Depends(get_db), user=Depends(provisioner)):
    item = service.get(db, staff_id, user, True)
    limiter.auth(request, 'staff-resend', item.user.email)
    if item.link_confirmed and not item.user.is_active:
        raise HTTPException(409, 'Activate the staff account before sending another setup invitation.')
    service.send_invitation(db, item)
    db.commit()
    return {'message': 'Staff invitation sent.'}
