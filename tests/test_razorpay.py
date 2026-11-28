"""Exercise the real routes, signatures, locks and ledger on disposable PostgreSQL."""
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.core.security import now
from app.modules.auth.models import User
from app.modules.finance import razorpay_gateway, razorpay_router, razorpay_service
from app.modules.finance.business_date import business_today
from app.modules.finance.models import Payment, PaymentOrder, RazorpayEvent
from app.modules.finance.razorpay_gateway import GatewayUnavailable, ProviderOrder, ProviderPayment, RazorpayGateway
from app.modules.tenants.models import Tenant
from conftest import authorization, sign_in
from test_finance import finance, post  # noqa: F401: fixtures shared across finance tests


BASE = '/api/v1/finance/'
CHECKOUT = BASE + 'me/razorpay/'
WEBHOOK = BASE + 'razorpay/webhook'
KEY = 'isolated-test-secret'
WEBHOOK_KEY = 'isolated-webhook-test-secret'


def sign(message, secret=KEY):
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@pytest.fixture
def online(finance, monkeypatch):
    c, db, admin, payload = finance
    settings = get_settings().model_copy(update=dict(razorpay_enabled=True, razorpay_key_id=SecretStr('rzp_test_QA123'),
        razorpay_key_secret=SecretStr(KEY), razorpay_webhook_secret=SecretStr(WEBHOOK_KEY)))
    for module in (razorpay_gateway, razorpay_router, razorpay_service):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    with db() as session:
        tenant = session.get(Tenant, UUID(payload['tenantId']))
        tenant.user_id = session.scalar(select(User.id).where(User.email == 'tenant@example.com'))
        session.commit()
    owner = authorization(sign_in(c, 'owner@example.com'))
    tenant = authorization(sign_in(c))
    invoice = post(c, admin, 'invoices', payload | {'discount': 2003.25}).json()
    provider_orders, provider_payments = {}, {}

    def create_order(self, receipt, amount):
        order = ProviderOrder(id=f'order_QA{len(provider_orders) + 1}', receipt=receipt, amount=amount, currency='INR')
        provider_orders[order.id] = order
        return order

    monkeypatch.setattr(RazorpayGateway, 'create_order', create_order)
    monkeypatch.setattr(RazorpayGateway, 'find_order', lambda self, receipt: next(
        (order for order in provider_orders.values() if order.receipt == receipt), None))
    monkeypatch.setattr(RazorpayGateway, 'fetch_order', lambda self, order_id: provider_orders[order_id])
    monkeypatch.setattr(RazorpayGateway, 'fetch_payment', lambda self, payment_id: provider_payments[payment_id])
    monkeypatch.setattr(RazorpayGateway, 'order_payments', lambda self, order_id: [
        payment for payment in provider_payments.values() if payment.order_id == order_id])
    return c, db, admin, tenant, owner, invoice, provider_orders, provider_payments, settings


def create(c, tenant, invoice, **extra):
    return c.post(CHECKOUT + 'orders', headers=tenant, json={'invoiceId': invoice['id'], **extra})


def provider_payment(order, **updates):
    return ProviderPayment.model_validate(dict(id='pay_Checkout1', order_id=order['providerOrderId'],
        amount=order['amountPaise'], currency='INR', status='captured', captured=True, method='upi',
        created_at=int(now().timestamp())) | updates)


def callback(order, payment_id='pay_Checkout1'):
    order_id = order['providerOrderId']
    return dict(attemptId=order['id'], razorpay_order_id=order_id, razorpay_payment_id=payment_id,
        razorpay_signature=sign(f'{order_id}|{payment_id}'.encode()))


def send_event(c, payment, *, event='payment.captured', event_id=None, signature=True):
    raw = json.dumps({'event': event, 'payload': {'payment': {'entity': payment.model_dump()}}}, indent=2).encode()
    headers = {'X-Razorpay-Event-Id': event_id or uuid4().hex}
    if signature is not None:
        headers['X-Razorpay-Signature'] = sign(raw, WEBHOOK_KEY) if signature else '0' * 64
    return c.post(WEBHOOK, content=raw, headers=headers)


def manual(c, admin, invoice, amount):
    return post(c, admin, 'payments', dict(tenantId=invoice['tenantId'], invoiceId=invoice['id'],
        amount=amount, paymentDate=str(business_today()), method='CASH'))


def test_test_only_settings():
    common = dict(_env_file=None, database_url='postgresql+psycopg://ignored', jwt_secret_key='a' * 32)
    assert Settings(**common, razorpay_enabled=True, razorpay_key_id='rzp_test_QA123', razorpay_key_secret=KEY).razorpay_ready
    assert not Settings(**common, razorpay_enabled=True, razorpay_key_id='rzp_test_QA123').razorpay_ready
    assert not Settings(**common, razorpay_enabled=False).razorpay_ready
    for bad in ({'razorpay_key_id': 'rzp_live_REJECT123'}, {'razorpay_mode': 'live'}):
        with pytest.raises(ValidationError):
            Settings(**common, **bad)


def test_partial_payment_checkout_receipt_and_reporting(online):
    c, db, admin, tenant, _, invoice, orders, payments, settings = online
    settings.razorpay_webhook_secret = SecretStr('')
    assert manual(c, admin, invoice, 500.25).json()['source'] == 'MANUAL'
    assert c.get(CHECKOUT + 'config', headers=tenant).json()['available']
    result = create(c, tenant, invoice)
    assert result.status_code == 200, result.text
    order = result.json()
    assert order['amountPaise'] == 749650 and order['keyId'].startswith('rzp_test_')
    assert KEY not in result.text
    assert create(c, tenant, invoice).json()['id'] == order['id'] and len(orders) == 1
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
    assert c.get(CHECKOUT + 'orders/' + order['id'], headers=tenant).json()['status'] == 'OPEN'
    provider = provider_payment(order)
    payments[provider.id] = provider
    verify = c.post(CHECKOUT + 'verify', headers=tenant, json=callback(order))
    assert verify.status_code == 200, verify.text
    saved = verify.json()
    assert saved['status'] == 'APPLIED'
    assert c.post(CHECKOUT + 'verify', headers=tenant, json=callback(order)).json()['paymentId'] == saved['paymentId']
    detail = c.get(BASE + 'invoices/' + invoice['id'], headers=tenant).json()
    assert (detail['status'], detail['paidAmount'], detail['outstandingAmount']) == ('PAID', 7996.75, 0)
    assert create(c, tenant, invoice).status_code == 409
    path = BASE + 'payments/' + saved['paymentId']
    receipt = c.get(path + '/receipt', headers=tenant).json()
    assert (receipt['reference'], receipt['source'], receipt['method'], receipt['isTest']) == (provider.id, 'RAZORPAY_TEST', 'UPI', True)
    assert c.post(path + '/refund', headers=admin, json={'reason': 'not a real refund'}).status_code == 409
    totals = c.get(BASE + 'overview?propertyId=prop-sunrise', headers=admin).json()
    assert (totals['collected'], totals['testCollected'], totals['outstanding']) == (500.25, 7496.5, 0)
    assert c.get('/api/v1/properties/prop-sunrise', headers=admin).json()['monthlyRevenue'] == 500.25
    assert c.get(BASE + 'payments?source=RAZORPAY_TEST', headers=admin).json()['total'] == 1


def test_disabled_ownership_and_ambiguous_order_recovery(online, monkeypatch):
    c, _, admin, tenant, owner, invoice, orders, _, settings = online
    settings.razorpay_enabled = False
    assert not c.get(CHECKOUT + 'config', headers=tenant).json()['available']
    assert create(c, tenant, invoice).status_code == 503
    assert manual(c, admin, invoice, 1).status_code == 200
    settings.razorpay_enabled = True
    assert create(c, owner, invoice).status_code == 403
    assert create(c, tenant, {'id': str(uuid4())}).status_code == 404
    assert create(c, tenant, invoice, amount=1).status_code == 422
    assert c.post(CHECKOUT + 'orders', headers=tenant | {'Origin': 'https://untrusted.example'}, json={'invoiceId': invoice['id']}).status_code == 403

    original = RazorpayGateway.create_order
    def timeout_after_acceptance(self, receipt, amount):
        original(self, receipt, amount)
        raise GatewayUnavailable
    monkeypatch.setattr(RazorpayGateway, 'create_order', timeout_after_acceptance)
    first = create(c, tenant, invoice).json()
    assert first['status'] == 'CREATING' and first['providerOrderId'] is None
    assert create(c, tenant, invoice).json()['id'] == first['id'] and len(orders) == 1
    status = c.post(CHECKOUT + f"orders/{first['id']}/recheck", headers=tenant).json()
    assert status['status'] == 'OPEN' and status['providerOrderId']


@pytest.mark.parametrize('changes', [{'order_id': 'order_wrong'}, {'amount': 1}, {'currency': 'USD'}, {'id': 'pay_wrong'}])
def test_reject_mismatched_provider_payment(online, changes):
    c, db, _, tenant, _, invoice, _, payments, _ = online
    order = create(c, tenant, invoice).json()
    payments['pay_Checkout1'] = provider_payment(order, **changes)
    result = c.post(CHECKOUT + 'verify', headers=tenant, json=callback(order))
    assert result.status_code == 409
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_failed_authorized_and_cancelled(online):
    c, _, admin, tenant, _, invoice, _, payments, _ = online
    order = create(c, tenant, invoice).json()
    values = callback(order)
    assert c.post(CHECKOUT + 'verify', headers=tenant, json=values | {'razorpay_signature': '0' * 64}).status_code == 400
    assert c.post(CHECKOUT + 'verify', headers=tenant, json=values | {'razorpay_order_id': 'order_other'}).status_code == 409
    for stage, status in [('failed', 'FAILED'), ('authorized', 'PENDING')]:
        payments['pay_Checkout1'] = provider_payment(order, status=stage, captured=False)
        assert c.post(CHECKOUT + 'verify', headers=tenant, json=values).json()['status'] == status
    assert c.post(BASE + 'invoices/' + invoice['id'] + '/cancel', headers=admin).status_code == 200
    assert create(c, tenant, invoice).status_code == 409


def test_signed_webhook_and_concurrent_verify(online):
    c, db, _, tenant, _, invoice, orders, payments, _ = online
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: create(c, tenant, invoice), range(2)))
    assert all(r.status_code == 200 for r in attempts)
    assert len(orders) == 1
    order = next(r.json() for r in attempts if r.json()['providerOrderId'])
    provider = provider_payment(order)
    payments[provider.id] = provider
    assert send_event(c, provider, signature=None).status_code == 401
    assert send_event(c, provider, signature=False).status_code == 401
    assert send_event(c, provider.model_copy(update={'amount': 10})).status_code == 409
    with ThreadPoolExecutor(max_workers=2) as pool:
        event = pool.submit(send_event, c, provider, event_id='evt_original')
        verify = pool.submit(c.post, CHECKOUT + 'verify', headers=tenant, json=callback(order))
        assert event.result().status_code == 200 and verify.result().json()['status'] == 'APPLIED'
    assert send_event(c, provider, event_id='evt_original').status_code == 200
    assert send_event(c, provider, event='order.paid').status_code == 200
    late_failure = provider_payment(order, id='pay_Declined', status='failed', captured=False)
    payments[late_failure.id] = late_failure
    assert send_event(c, late_failure, event='payment.failed').status_code == 200
    assert c.post(CHECKOUT + 'verify', headers=tenant, json=callback(order, late_failure.id)).status_code == 409
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
        assert session.scalar(select(func.count()).select_from(RazorpayEvent)) == 3
        assert session.scalar(select(PaymentOrder.status)) == 'APPLIED'


def test_another_tenant_cannot_read_or_verify_and_unrelated_event_is_ignored(online):
    c, db, _, tenant, _, invoice, orders, payments, _ = online
    order = create(c, tenant, invoice).json()
    with db() as session:
        original = session.scalar(select(User).where(User.email == 'tenant@example.com'))
        stranger = User(full_name='Another QA Tenant', email='other-qa@example.com',
            password_hash=original.password_hash, role_id='TENANT')
        session.add(stranger)
        session.flush()
        session.add(Tenant(email=stranger.email, property_id=invoice['propertyId'], user_id=stranger.id,
            status='ONBOARDING', source='DIRECT', data={'firstName': 'Another', 'lastName': 'Tenant'}))
        session.commit()
    other = authorization(sign_in(c, 'other-qa@example.com'))
    assert create(c, other, invoice).status_code == 404
    assert c.get(CHECKOUT + 'orders/' + order['id'], headers=other).status_code == 404
    assert c.post(CHECKOUT + 'verify', headers=other, json=callback(order)).status_code == 404
    assert c.get(BASE + 'razorpay/attempts', headers=other, params={'invoiceId': invoice['id']}).status_code == 404
    unrelated = ProviderOrder(id='order_Unrelated', receipt='other-integration', amount=100, currency='INR')
    orders[unrelated.id] = unrelated
    provider = provider_payment(order, id='pay_Unrelated', order_id=unrelated.id, amount=100)
    payments[provider.id] = provider
    assert send_event(c, provider).status_code == 200
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_manual_balance_change_preserves_unapplied_capture(online):
    c, db, admin, tenant, owner, invoice, _, payments, _ = online
    order = create(c, tenant, invoice).json()
    assert manual(c, admin, invoice, 10).status_code == 200
    provider = provider_payment(order)
    payments[provider.id] = provider
    result = c.post(CHECKOUT + f"orders/{order['id']}/recheck", headers=tenant).json()
    assert result['status'] == 'RECONCILIATION_REQUIRED' and result['providerPaymentId'] == provider.id
    assert result['paymentId'] is None
    assert create(c, tenant, invoice).status_code == 409
    assert len(c.get(BASE + 'razorpay/attempts', headers=admin).json()) == 1
    assert c.get(BASE + 'razorpay/attempts', headers=owner).json() == []
    assert c.get(BASE + 'invoices/' + invoice['id'], headers=tenant).json()['outstandingAmount'] == 7986.75
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1
