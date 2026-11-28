"""Durable test orders and a single finalization path for Checkout and webhooks."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import lazyload

from app.core.config import get_settings
from app.core.security import now
from app.modules.finance import service as finance
from app.modules.finance.business_date import BUSINESS_TIMEZONE, business_today
from app.modules.finance.models import Invoice, PaymentOrder, RazorpayEvent
from app.modules.finance.razorpay_gateway import GatewayUnavailable, RazorpayGateway


TERMINAL = ('APPLIED', 'RECONCILIATION_REQUIRED')
REUSABLE = ('CREATING', 'OPEN', 'PENDING', 'FAILED')
METHODS = {'upi': 'UPI', 'card': 'CARD', 'netbanking': 'BANK_TRANSFER', 'bank_transfer': 'BANK_TRANSFER'}


def tenant_only(user):
    if user.role_id != 'TENANT':
        raise HTTPException(403, 'A linked tenant account is required.')
    finance.authorize(user, 'invoices')


def receipt(attempt_id):
    return 'stayops-' + attempt_id.hex


def output(attempt):
    return dict(id=attempt.id, invoiceId=attempt.invoice_id, propertyId=attempt.property_id,
        amountPaise=attempt.amount_paise, currency=attempt.currency, status=attempt.status,
        providerOrderId=attempt.provider_order_id, providerPaymentId=attempt.provider_payment_id,
        paymentId=attempt.finance_payment_id, detail=attempt.failure_reason, isTest=True,
        createdAt=attempt.created_at)


def attach_order(db, attempt_id, provider):
    attempt = db.get(PaymentOrder, attempt_id, with_for_update=True, populate_existing=True)
    if (provider.receipt != receipt(attempt.id) or provider.currency != attempt.currency
            or provider.amount != attempt.amount_paise
            or attempt.provider_order_id not in (None, provider.id)):
        raise GatewayUnavailable
    if not attempt.provider_order_id:
        attempt.provider_order_id = provider.id
        attempt.status = 'OPEN'
        attempt.failure_reason = None
    db.commit()
    return attempt


def complete_order_creation(db, attempt, provider):
    try:
        return attach_order(db, attempt.id, provider)
    except GatewayUnavailable:
        raise HTTPException(503, 'Razorpay order confirmation is unavailable. Check the existing attempt later.') from None


def create_order(db, user, invoice_id):
    tenant_only(user)
    gateway = RazorpayGateway()
    invoice = finance.get(db, Invoice, invoice_id, user, lock=True)
    balance = finance.invoice_balance(db, invoice)
    if invoice.issue_date > business_today() or balance <= 0:
        raise HTTPException(409, 'This invoice is not eligible for online payment.')
    amount = int(balance * 100)
    if amount < 100:
        raise HTTPException(422, 'Razorpay requires at least ₹1. Contact your property office for smaller balances.')

    history = select(PaymentOrder).where(PaymentOrder.invoice_id == invoice.id)
    blocking = db.scalar(history.where(PaymentOrder.status == 'RECONCILIATION_REQUIRED').limit(1))
    if blocking:
        raise HTTPException(409, 'A Test Mode payment needs review. Contact your property office.')
    attempt = db.scalar(history.where(PaymentOrder.amount_paise == amount, PaymentOrder.status.in_(REUSABLE)))
    another_pending = db.scalar(history.where(PaymentOrder.amount_paise != amount,
        PaymentOrder.status.in_(('CREATING', 'PENDING'))).limit(1))
    if another_pending:
        raise HTTPException(409, 'Check the earlier Test Mode payment status before opening another order.')
    new = attempt is None
    if new:
        attempt = PaymentOrder(invoice_id=invoice.id, tenant_id=invoice.tenant_id,
            property_id=invoice.property_id, initiated_by=user.id, amount_paise=amount)
        db.add(attempt)
    # Serialize reservation under the invoice lock; release it before calling Razorpay.
    db.commit()
    if new:
        try:
            provider = gateway.create_order(receipt(attempt.id), amount)
        except GatewayUnavailable:
            # The provider may have accepted the POST. A retry may only look up this receipt.
            return output(attempt)
        attempt = complete_order_creation(db, attempt, provider)

    # Staff can record a manual payment while the provider call is in progress.
    current = finance.get(db, Invoice, invoice_id, user)
    launchable = (attempt.status in ('OPEN', 'FAILED') and attempt.provider_order_id
        and finance.invoice_balance(db, current) * 100 == attempt.amount_paise)
    result = output(attempt)
    if launchable:
        result.update(keyId=get_settings().razorpay_key_id.get_secret_value(), invoiceNumber=current.invoice_number)
    elif attempt.status in ('OPEN', 'FAILED') and attempt.provider_order_id:
        result['detail'] = 'The invoice balance changed. Reload the invoice before retrying.'
    return result


def process_payment(db, attempt, provider):
    """Apply verified provider state under the same invoice lock used by manual payments."""
    if provider.order_id != attempt.provider_order_id:
        raise HTTPException(409, 'Razorpay payment does not match the expected order.')
    if provider.amount != attempt.amount_paise or provider.currency != attempt.currency:
        raise HTTPException(409, 'Razorpay payment amount or currency does not match the order.')

    invoice = db.get(Invoice, attempt.invoice_id, with_for_update=True, populate_existing=True,
        options=[lazyload(Invoice.items)])
    attempt = db.get(PaymentOrder, attempt.id, with_for_update=True, populate_existing=True)
    if attempt.status in TERMINAL:
        return attempt
    if provider.status == 'failed':
        if attempt.status != 'PENDING' or attempt.provider_payment_id == provider.id:
            attempt.status = 'FAILED'
            attempt.provider_payment_id = provider.id
            attempt.failure_reason = 'Razorpay declined the payment. You can retry the existing order.'
        return attempt
    if provider.status in ('created', 'authorized'):
        attempt.status = 'PENDING'
        attempt.provider_payment_id = provider.id
        attempt.failure_reason = None
        return attempt
    if not provider.captured:
        raise HTTPException(503, 'Razorpay capture could not be confirmed. Check again later.')

    attempt.provider_payment_id = provider.id
    attempt.completed_at = now()
    amount = Decimal(provider.amount) / 100
    paid_on = datetime.fromtimestamp(provider.created_at, BUSINESS_TIMEZONE).date()
    if (provider.status == 'refunded' or provider.amount_refunded or invoice.cancelled
            or amount != finance.invoice_balance(db, invoice) or not invoice.issue_date <= paid_on <= business_today()):
        attempt.status = 'RECONCILIATION_REQUIRED'
        attempt.failure_reason = 'Captured test payment needs review. The invoice balance was not changed.'
        return attempt

    payment = finance.add_successful_payment(db, invoice, attempt.initiated_by, amount, paid_on,
        METHODS.get(provider.method, 'OTHER'), provider.id, '', 'razorpay:' + provider.id,
        attempt.id.hex, source='RAZORPAY_TEST')
    db.flush()
    attempt.finance_payment_id = payment.id
    attempt.status = 'APPLIED'
    attempt.failure_reason = None
    return attempt


def verify(db, user, values):
    tenant_only(user)
    attempt = finance.get(db, PaymentOrder, values.attemptId, user)
    if not attempt.provider_order_id or attempt.provider_order_id != values.razorpay_order_id:
        raise HTTPException(409, 'Razorpay checkout does not match the expected order.')
    gateway = RazorpayGateway()
    gateway.verify_checkout(attempt.provider_order_id, values.razorpay_payment_id, values.razorpay_signature)
    if attempt.status in TERMINAL and attempt.provider_payment_id == values.razorpay_payment_id:
        return output(attempt)
    db.commit()
    provider = gateway.fetch_payment(values.razorpay_payment_id)
    if provider.id != values.razorpay_payment_id:
        raise HTTPException(409, 'Razorpay returned a different payment.')
    attempt = process_payment(db, attempt, provider)
    if attempt.status in TERMINAL and attempt.provider_payment_id != provider.id:
        raise HTTPException(409, 'This order was finalized with a different payment.')
    db.commit()
    return output(attempt)


def recheck(db, user, attempt_id):
    tenant_only(user)
    attempt = finance.get(db, PaymentOrder, attempt_id, user)
    if attempt.status in TERMINAL:
        return output(attempt)
    gateway = RazorpayGateway()
    db.commit()
    if not attempt.provider_order_id:
        provider = gateway.find_order(receipt(attempt.id))
        if not provider:
            return output(attempt)
        attempt = attach_order(db, attempt.id, provider)
    payments = gateway.order_payments(attempt.provider_order_id)
    priority = {'captured': 3, 'refunded': 3, 'authorized': 2, 'created': 1, 'failed': 0}
    if payments:
        provider = max(payments, key=lambda p: (priority[p.status], p.created_at, p.id))
        attempt = process_payment(db, attempt, provider)
        db.commit()
    return output(attempt)


def webhook(db, event_id, event_type, entity):
    order_id, payment_id = entity.order_id, entity.id
    if db.get(RazorpayEvent, event_id):
        return
    attempt = db.scalar(select(PaymentOrder).where(PaymentOrder.provider_order_id == order_id))
    db.commit()
    gateway = RazorpayGateway()
    if attempt is None:
        provider_order = gateway.fetch_order(order_id)
        # Some orders belong to other integrations on the same test account.
        local_id = provider_order.receipt.removeprefix('stayops-')
        if provider_order.receipt.startswith('stayops-') and len(local_id) == 32:
            try:
                attempt = db.get(PaymentOrder, UUID(hex=local_id))
            except ValueError:
                pass
            if attempt:
                attempt = attach_order(db, attempt.id, provider_order)
        db.commit()
    if attempt:
        provider = gateway.fetch_payment(payment_id)
        if (provider.id != payment_id or provider.order_id != order_id
                or provider.amount != entity.amount or provider.currency != entity.currency):
            raise HTTPException(409, 'Razorpay event does not match the verified payment.')
        if event_type in ('payment.captured', 'order.paid') and provider.status not in ('captured', 'refunded'):
            raise HTTPException(503, 'Razorpay capture is not yet confirmed.')
        if event_type == 'payment.failed' and provider.status not in ('failed', 'captured', 'refunded'):
            raise HTTPException(503, 'Razorpay payment outcome is not yet confirmed.')

    claimed = db.scalar(insert(RazorpayEvent).values(event_id=event_id, event_type=event_type)
        .on_conflict_do_nothing().returning(RazorpayEvent.event_id))
    if claimed and attempt:
        process_payment(db, attempt, provider)
    db.commit()
