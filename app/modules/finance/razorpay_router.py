"""Tenant Checkout routes and a signed-only Razorpay webhook."""
import hashlib
import hmac
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, require_trusted_origin
from app.modules.finance import razorpay_service as online, service as finance
from app.modules.finance.models import Invoice, PaymentOrder
from app.modules.finance.razorpay_gateway import GatewayUnavailable
from app.modules.tenants.models import Tenant


OrderId = Annotated[str, Field(pattern=r'^order_[A-Za-z0-9]{1,55}$')]
PaymentId = Annotated[str, Field(pattern=r'^pay_[A-Za-z0-9]{1,59}$')]
Signature = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class CreateOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invoiceId: UUID


class VerifyOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attemptId: UUID
    razorpay_order_id: OrderId
    razorpay_payment_id: PaymentId
    razorpay_signature: Signature


class EventPayment(BaseModel):
    id: PaymentId
    order_id: OrderId | None
    amount: int = Field(strict=True, gt=0)
    currency: str = Field(min_length=3, max_length=3)


class EventEnvelope(BaseModel):
    event: Literal['payment.captured', 'payment.failed', 'order.paid']
    payload: dict


class GatewayRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except GatewayUnavailable:
                raise HTTPException(503, 'Razorpay confirmation is unavailable. Check the existing attempt later.') from None

        return handle


router = APIRouter(route_class=GatewayRoute)
write = [Depends(require_trusted_origin)]


@router.get('/me/razorpay/config')
def tenant_config(db: Session = Depends(get_db), user=Depends(get_active_user)):
    online.tenant_only(user)
    linked = db.scalar(select(Tenant.id).where(Tenant.user_id == user.id)) is not None
    return {'available': linked and get_settings().razorpay_ready, 'isTest': True}


@router.post('/me/razorpay/orders', dependencies=write)
def create_order(values: CreateOrder, db: Session = Depends(get_db), user=Depends(get_active_user)):
    limiter.check(f'razorpay:create:{user.id}', 12, 60)
    return online.create_order(db, user, values.invoiceId)


@router.get('/me/razorpay/orders/{attempt_id}')
def read_order(attempt_id: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    online.tenant_only(user)
    return online.output(finance.get(db, PaymentOrder, attempt_id, user))


@router.post('/me/razorpay/orders/{attempt_id}/recheck', dependencies=write)
def recheck_order(attempt_id: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    limiter.check(f'razorpay:recheck:{user.id}', 20, 60)
    return online.recheck(db, user, attempt_id)


@router.post('/me/razorpay/verify', dependencies=write)
def verify_order(values: VerifyOrder, db: Session = Depends(get_db), user=Depends(get_active_user)):
    limiter.check(f'razorpay:verify:{user.id}', 24, 60)
    return online.verify(db, user, values)


@router.get('/razorpay/attempts')
def list_attempts(invoiceId: UUID | None = None, limit: int = Query(20, ge=1, le=100),
                  offset: int = Query(0, ge=0), db: Session = Depends(get_db), user=Depends(get_active_user)):
    finance.authorize(user, 'payments')
    stmt = finance.scope(select(PaymentOrder), PaymentOrder, user)
    if invoiceId:
        finance.get(db, Invoice, invoiceId, user)
        stmt = stmt.where(PaymentOrder.invoice_id == invoiceId)
    else:
        if user.role_id == 'TENANT':
            raise HTTPException(422, 'Select an invoice to view its test attempts.')
        stmt = stmt.where(PaymentOrder.status == 'RECONCILIATION_REQUIRED')
    return [online.output(item) for item in db.scalars(stmt.order_by(PaymentOrder.created_at.desc(),
        PaymentOrder.id).offset(offset).limit(limit))]


async def signed_event(request: Request, x_razorpay_signature: Signature | None = Header(None),
                       x_razorpay_event_id: str | None = Header(None, max_length=128)):
    settings = get_settings()
    secret = settings.razorpay_webhook_secret.get_secret_value()
    if not settings.razorpay_ready or not secret.strip():
        raise HTTPException(503, 'Razorpay Test Mode webhook is unavailable.')
    if not x_razorpay_signature:
        raise HTTPException(401, 'Missing Razorpay webhook signature.')
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_razorpay_signature):
        raise HTTPException(401, 'Invalid Razorpay webhook signature.')
    if not x_razorpay_event_id or not x_razorpay_event_id.strip():
        raise HTTPException(422, 'Missing Razorpay webhook event ID.')
    try:
        event = EventEnvelope.model_validate_json(raw)
        payment = EventPayment.model_validate(event.payload['payment']['entity'])
    except (ValidationError, KeyError, TypeError):
        raise HTTPException(422, 'Invalid Razorpay webhook event.') from None
    if not payment.order_id and event.event != 'payment.failed':
        raise HTTPException(422, 'Razorpay event is missing its order.')
    limiter.check('razorpay:signed-webhook', 300, 60)
    return x_razorpay_event_id, event.event, payment


@router.post('/razorpay/webhook')
def webhook(event=Depends(signed_event), db: Session = Depends(get_db)):
    if event[2].order_id:
        online.webhook(db, *event)
    return {'received': True}
