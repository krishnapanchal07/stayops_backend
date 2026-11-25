from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, get_optional_user, require_permission, require_trusted_origin
from app.modules.inquiries import service
from app.modules.inquiries.models import Inquiry
from app.modules.inquiries.schemas import ConvertInput, GeneralInquiry, RoomInquiry, StatusInput

router = APIRouter(prefix='/inquiries', tags=['Inquiries'])


def filtered(user, query='', propertyId='', status=''):
    stmt = service.scoped(select(Inquiry), Inquiry, user)
    if propertyId:
        stmt = stmt.where(Inquiry.property_id == propertyId)
    if status:
        stmt = stmt.where(Inquiry.status == status)
    if query:
        needle = '%' + query.replace('%', r'\%').replace('_', r'\_') + '%'
        stmt = stmt.where(or_(Inquiry.email.ilike(needle), Inquiry.data['fullName'].as_string().ilike(needle),
            Inquiry.data['listingTitle'].as_string().ilike(needle), Inquiry.data['phone'].as_string().ilike(needle)))
    return stmt


def page(db, stmt, user, offset, limit):
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.scalars(stmt.order_by(Inquiry.created_at.desc(), Inquiry.id).offset(offset).limit(limit)).all()
    return {'items': [service.output(i, user.role_id == 'TENANT') for i in items], 'total': total, 'offset': offset, 'limit': limit}


@router.post('', status_code=201, dependencies=[Depends(require_trusted_origin)])
def create(payload: RoomInquiry, request: Request, db: Session = Depends(get_db), user=Depends(get_optional_user),
           key: str | None = Header(None, alias='Idempotency-Key', min_length=16, max_length=128)):
    limiter.auth(request, 'room-inquiry', str(payload.email))
    return service.output(service.create(db, payload, user, key))


@router.post('/general', status_code=201, dependencies=[Depends(require_trusted_origin)])
def general(payload: GeneralInquiry, request: Request, db: Session = Depends(get_db), user=Depends(get_optional_user),
            key: str | None = Header(None, alias='Idempotency-Key', min_length=16, max_length=128)):
    limiter.auth(request, 'general-inquiry', str(payload.email))
    return service.output(service.create(db, payload, user, key, general=True))


@router.get('')
def listing(db: Session = Depends(get_db), user=Depends(require_permission('INQUIRY_VIEW')),
            offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
            query: str = Query('', max_length=120), propertyId: str = Query('', max_length=64), status: str = Query('', max_length=24)):
    return page(db, filtered(user, query, propertyId, status), user, offset, limit)


@router.get('/summary')
def summary(db: Session = Depends(get_db), user=Depends(require_permission('INQUIRY_VIEW'))):
    sub = filtered(user).subquery()
    counts = dict(db.execute(select(sub.c.status, func.count()).group_by(sub.c.status)).all())
    return {'total': sum(counts.values()), **{s.lower(): counts.get(s, 0) for s in
        ('SUBMITTED', 'CONTACTED', 'SHORTLISTED', 'APPROVED', 'CONVERTED', 'REJECTED', 'WITHDRAWN')}}


@router.get('/availability')
def availability(db: Session = Depends(get_db)):
    from app.modules.inventory.service import availability as inventory_availability
    return inventory_availability(db)


@router.get('/me')
def mine(db: Session = Depends(get_db), user=Depends(get_active_user),
         offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    return page(db, select(Inquiry).where(service.ownership(user)), user, offset, limit)


@router.get('/{inquiry_id}')
def detail(inquiry_id: UUID, db: Session = Depends(get_db), user=Depends(require_permission('INQUIRY_VIEW'))):
    return service.output(service.get(db, inquiry_id, user), user.role_id == 'TENANT')


@router.patch('/{inquiry_id}/status', dependencies=[Depends(require_trusted_origin)])
def change(inquiry_id: UUID, payload: StatusInput, db: Session = Depends(get_db), user=Depends(require_permission('INQUIRY_MANAGE'))):
    return service.output(service.change(db, service.get(db, inquiry_id, user, True), payload.status, user, payload.note))


@router.post('/{inquiry_id}/withdraw', dependencies=[Depends(require_trusted_origin)])
def withdraw(inquiry_id: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    item = service.get(db, inquiry_id, user, True)
    if not service.owns(db, item, user):
        raise HTTPException(403, 'Only the submitting account can withdraw this inquiry.')
    return service.output(service.change(db, item, 'WITHDRAWN', user), True)


@router.post('/{inquiry_id}/convert', dependencies=[Depends(require_trusted_origin)])
def convert(inquiry_id: UUID, payload: ConvertInput, db: Session = Depends(get_db),
            user=Depends(require_permission('INQUIRY_MANAGE', 'TENANT_CREATE'))):
    return service.convert(db, service.get(db, inquiry_id, user, True), user, payload.allocateBed)
