from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, require_permission, require_role, require_trusted_origin
from app.modules.inquiries.service import scoped
from app.modules.tenants import service
from app.modules.tenants.models import Tenant, TenantActivity
from app.modules.tenants.schemas import AcceptInvitation, AllocateInput, CheckoutInput, PersonalInput, StatusInput, TenantPatch, TenantWrite

router = APIRouter(prefix='/tenants', tags=['Tenants'])


@router.get('')
def listing(db: Session = Depends(get_db), user=Depends(require_permission('TENANT_VIEW')),
            offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
            query: str = Query('', max_length=120), propertyId: str = Query('', max_length=64),
            status: str = Query('', max_length=24), roomId: str = Query('', max_length=64)):
    stmt = scoped(select(Tenant), Tenant, user)
    for column, value in ((Tenant.property_id, propertyId), (Tenant.status, status), (Tenant.room_id, roomId)):
        if value:
            stmt = stmt.where(column == value)
    if query:
        needle = '%' + query.replace('%', r'\%').replace('_', r'\_') + '%'
        stmt = stmt.where(or_(Tenant.email.ilike(needle), Tenant.tenant_code.ilike(needle),
            Tenant.data['firstName'].as_string().ilike(needle), Tenant.data['lastName'].as_string().ilike(needle),
            Tenant.data['phone'].as_string().ilike(needle)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.scalars(stmt.order_by(Tenant.created_at.desc(), Tenant.id).offset(offset).limit(limit)).all()
    return {'items': [service.output(t) for t in items], 'total': total, 'offset': offset, 'limit': limit}


@router.post('', status_code=201, dependencies=[Depends(require_trusted_origin)])
def create(payload: TenantWrite, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_CREATE'))):
    return service.output(service.create(db, payload, user))


@router.get('/summary')
def summary(db: Session = Depends(get_db), user=Depends(require_permission('TENANT_VIEW'))):
    sub = scoped(select(Tenant), Tenant, user).subquery()
    counts = dict(db.execute(select(sub.c.status, func.count()).group_by(sub.c.status)).all())
    return {'totalTenants': sum(counts.values()), **{key: counts.get(status, 0) for key, status in
        [('activeTenants', 'ACTIVE'), ('onboardingTenants', 'ONBOARDING'), ('noticePeriodTenants', 'NOTICE_PERIOD'), ('checkedOutTenants', 'CHECKED_OUT')]}}


@router.get('/me')
def me(db: Session = Depends(get_db), user=Depends(get_active_user)):
    return service.output(service.me(db, user), True)


@router.patch('/me', dependencies=[Depends(require_trusted_origin)])
def update_me(payload: PersonalInput, db: Session = Depends(get_db), user=Depends(get_active_user)):
    item = service.me(db, user)
    if not item:
        raise HTTPException(404, 'Tenant onboarding profile has not been created yet.')
    item = service.get(db, item.id, user, True)
    if item.status == 'INACTIVE':
        raise HTTPException(403, 'Tenant profile is inactive.')
    item.data = dict(item.data) | payload.model_dump(mode='json')
    service.activity(db, item, user, 'Personal profile updated', 'Tenant updated permitted personal information.')
    db.commit()
    return service.output(item, True)


@router.post('/accept-invitation', dependencies=[Depends(require_trusted_origin)])
def accept(payload: AcceptInvitation, request: Request, db: Session = Depends(get_db), user=Depends(get_active_user)):
    limiter.auth(request, 'tenant-link', user.email)
    return service.accept(db, user, payload.token)


@router.get('/{tenant_id}')
def detail(tenant_id: UUID, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_VIEW'))):
    return service.output(service.get(db, tenant_id, user))


@router.patch('/{tenant_id}', dependencies=[Depends(require_trusted_origin)])
def update(tenant_id: UUID, payload: TenantPatch, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_EDIT'))):
    return service.output(service.update(db, service.get(db, tenant_id, user, True), payload, user))


@router.patch('/{tenant_id}/status', dependencies=[Depends(require_trusted_origin)])
def status(tenant_id: UUID, payload: StatusInput, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_EDIT'))):
    item = service.get(db, tenant_id, user, True)
    service.status(db, item, payload.status, user)
    db.commit()
    return service.output(item)


@router.post('/{tenant_id}/allocate', dependencies=[Depends(require_trusted_origin)])
def allocate(tenant_id: UUID, payload: AllocateInput, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_EDIT', 'ROOM_ALLOCATE'))):
    item = service.get(db, tenant_id, user, True)
    service.allocate(db, item, payload.roomId, payload.bedId, user, payload.moveInDate)
    db.commit()
    return service.output(item)


@router.post('/{tenant_id}/checkout', dependencies=[Depends(require_trusted_origin)])
def checkout(tenant_id: UUID, payload: CheckoutInput, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_EDIT'))):
    item = service.get(db, tenant_id, user, True)
    service.status(db, item, 'CHECKED_OUT', user, payload.moveOutDate)
    db.commit()
    return service.output(item)


@router.get('/{tenant_id}/activity')
def activity(tenant_id: UUID, db: Session = Depends(get_db), user=Depends(require_permission('TENANT_VIEW')),
             offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    service.get(db, tenant_id, user)
    stmt = select(TenantActivity).where(TenantActivity.tenant_id == tenant_id).order_by(TenantActivity.occurred_at.desc()).offset(offset).limit(limit)
    return [{'id': str(e.id), 'action': e.action, 'description': e.description, 'actor': e.actor_name,
        'occurredAt': e.occurred_at.isoformat()} for e in db.scalars(stmt)]


@router.post('/{tenant_id}/invite', dependencies=[Depends(require_trusted_origin)])
def invite(tenant_id: UUID, request: Request, db: Session = Depends(get_db), user=Depends(require_role('SUPER_ADMIN'))):
    limiter.auth(request, 'tenant-invite', str(user.id))
    return service.invite(db, service.get(db, tenant_id, user, True), user)
