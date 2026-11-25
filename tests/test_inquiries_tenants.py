from app.modules.properties.models import Property
"""Phase 2 tests run only on the disposable PostgreSQL cluster from conftest."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import authorization, sign_in
from app.modules.auth.models import PropertyAssignment, User
from app.modules.inquiries import service as inquiries
from app.modules.inquiries.models import Inquiry, InquiryStatusEvent
from app.modules.tenants.models import Occupancy, Tenant, TenantActivity
from app.modules.tenants import service as tenants

ROOM = {'firstName': 'Ada', 'lastName': 'Shah', 'email': 'ada@example.com', 'phone': '9876543210',
    'roomId': 'room-sunrise-205', 'bedId': 'bed-205-b', 'message': 'Please arrange a viewing.'}
PROFILE = {'firstName': 'Ada', 'lastName': 'Shah', 'email': 'ada@example.com', 'phone': '9876543210',
    'propertyId': 'prop-sunrise', 'internalNotes': 'Private operations note'}


def admin(client):
    return authorization(sign_in(client, 'admin@example.com'))


def approved(client, headers, **changes):
    response = client.post('/api/v1/inquiries', json=ROOM | changes)
    assert response.status_code == 201, response.text
    item = response.json()
    for status in ('CONTACTED', 'SHORTLISTED', 'APPROVED'):
        response = client.patch(f"/api/v1/inquiries/{item['id']}/status", json={'status': status}, headers=headers)
        assert response.status_code == 200, response.text
    return response.json()


def test_guest_submission_and_validation(client):
    result = client.post('/api/v1/inquiries', json=ROOM)
    assert result.status_code == 201, result.text
    item = result.json()
    assert item['userId'] is None and item['monthlyRent'] == 8000
    assert item['statusHistory'][0]['from'] is None
    assert client.post('/api/v1/inquiries', json=ROOM).status_code == 409
    for patch, code in [({'roomId': 'phantom'}, 422), ({'bedId': 'bed-205-a'}, 201),
        ({'bedId': 'bed-302-c'}, 422), ({'propertyId': 'prop-oasis'}, 422),
        ({'userId': str(UUID(int=1))}, 422), ({'status': 'APPROVED'}, 422),
        ({'phone': '123'}, 422), ({'message': 'x' * 2001}, 422)]:
        assert client.post('/api/v1/inquiries', json=ROOM | patch).status_code == code
    assert client.post('/api/v1/inquiries', json=ROOM, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/v1/inquiries').status_code == 401


def test_idempotency_general_and_rate_limit(client):
    payload = {'name': 'New Owner', 'email': 'lead@example.com', 'inquiryType': 'owner'}
    headers = {'Idempotency-Key': 'test-key-123456789'}
    a = client.post('/api/v1/inquiries/general', json=payload, headers=headers)
    b = client.post('/api/v1/inquiries/general', json=payload, headers=headers)
    assert a.status_code == b.status_code == 201
    assert a.json()['id'] == b.json()['id']
    assert a.json()['propertyId'] == '' and a.json()['kind'] == 'owner'
    assert client.post('/api/v1/inquiries/general', json=payload | {'name': 'Changed'}, headers=headers).status_code == 409
    for _ in range(8):
        response = client.post('/api/v1/inquiries/general', json=payload)
    assert response.status_code == 429
    assert client.get('/api/v1/inquiries', headers=admin(client)).json()['total'] >= 1


def test_identity_scope_and_withdrawal(setup):
    client, factory, *_ = setup
    tenant_headers = authorization(sign_in(client))
    result = client.post('/api/v1/inquiries', json=ROOM | {'email': 'tenant@example.com'}, headers=tenant_headers)
    assert result.status_code == 201
    item = result.json()
    assert len(client.get('/api/v1/inquiries/me', headers=tenant_headers).json()['items']) == 1
    guest = client.post('/api/v1/inquiries', json=ROOM).json()
    assert client.get(f"/api/v1/inquiries/{guest['id']}", headers=tenant_headers).status_code == 404
    assert client.post(f"/api/v1/inquiries/{guest['id']}/withdraw", headers=tenant_headers).status_code == 404
    assert client.post('/api/v1/inquiries', json=ROOM, headers=tenant_headers).status_code == 422
    owner_headers = authorization(sign_in(client, 'owner@example.com'))
    assert client.get('/api/v1/inquiries', headers=owner_headers).json()['total'] == 0
    assert client.get(f"/api/v1/inquiries/{item['id']}", headers=owner_headers).status_code == 403
    with factory() as db:
        owner = db.scalar(select(User).where(User.email == 'owner@example.com'))
        db.get(Property, 'prop-sunrise').owner_user_id = owner.id
        db.add(PropertyAssignment(user_id=owner.id, property_id='prop-sunrise'))
        db.commit()
    assert client.get('/api/v1/inquiries', headers=owner_headers).json()['total'] == 2
    assert client.post(f"/api/v1/inquiries/{item['id']}/withdraw", headers=tenant_headers).status_code == 200


@pytest.mark.parametrize('allocate', [False, True])
def test_conversion_and_checkout(setup, allocate):
    client, factory, *_ = setup
    headers = admin(client)
    item = approved(client, headers)
    response = client.post(f"/api/v1/inquiries/{item['id']}/convert", json={'allocateBed': allocate}, headers=headers)
    assert response.status_code == 200, response.text
    tenant = response.json()['tenant']
    assert bool(tenant['bedId']) == allocate
    assert tenant['status'] == 'ONBOARDING'
    assert client.post(f"/api/v1/inquiries/{item['id']}/convert", json={}, headers=headers).status_code == 409
    active = client.patch(f"/api/v1/tenants/{tenant['id']}/status", json={'status': 'ACTIVE'}, headers=headers)
    assert active.status_code == (200 if allocate else 409)
    response = client.post(f"/api/v1/tenants/{tenant['id']}/checkout", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['bedId'] is None and response.json()['actualMoveOutDate']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Occupancy)) == 0
        assert db.scalar(select(func.count()).select_from(Tenant)) == 1


def test_invalid_transition_and_rollback(setup, monkeypatch):
    client, factory, *_ = setup
    headers = admin(client)
    raw = client.post('/api/v1/inquiries', json=ROOM).json()
    assert client.patch(f"/api/v1/inquiries/{raw['id']}/status", json={'status': 'CONVERTED'}, headers=headers).status_code == 409
    for status in ('CONTACTED', 'SHORTLISTED', 'APPROVED'):
        assert client.patch(f"/api/v1/inquiries/{raw['id']}/status", json={'status': status}, headers=headers).status_code == 200
    original = tenants.allocate

    def failure(*args, **kwargs):
        original(*args, **kwargs)
        raise HTTPException(409, 'Simulated failure after ledger insert')

    monkeypatch.setattr(tenants, 'allocate', failure)
    result = client.post(f"/api/v1/inquiries/{raw['id']}/convert", json={'allocateBed': True}, headers=headers)
    assert result.status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Tenant)) == 0
        assert db.scalar(select(func.count()).select_from(Occupancy)) == 0
        assert db.get(Inquiry, UUID(raw['id'])).status == 'APPROVED'


def test_concurrent_conversion(setup):
    client, factory, *_ = setup
    headers = admin(client)
    item = approved(client, headers)

    def convert():
        with factory() as db:
            user = db.scalar(select(User).where(User.email == 'admin@example.com'))
            try:
                inquiry = inquiries.get(db, UUID(item['id']), user, True)
                inquiries.convert(db, inquiry, user, True)
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: convert(), range(2))) == [200, 409]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Tenant)) == 1
        assert db.scalar(select(func.count()).select_from(Occupancy)) == 1


def test_tenant_crud_allocation_conflict_and_scope(setup):
    client, factory, *_ = setup
    headers = admin(client)
    response = client.post('/api/v1/tenants', json=PROFILE, headers=headers)
    assert response.status_code == 201, response.text
    first = response.json()
    second = client.post('/api/v1/tenants', json=PROFILE | {'email': 'second@example.com'}, headers=headers).json()
    assert first['tenantCode'] != second['tenantCode']
    assert client.post('/api/v1/tenants', json=PROFILE, headers=headers).status_code == 409
    assert client.patch(f"/api/v1/tenants/{first['id']}", json={'phone': '9123456789'}, headers=headers).status_code == 200
    assert client.patch(f"/api/v1/tenants/{first['id']}", json={'userId': str(UUID(int=1))}, headers=headers).status_code == 422
    allocation = {'roomId': 'room-sunrise-205', 'bedId': 'bed-205-b', 'moveInDate': '2026-11-23'}
    assert client.post(f"/api/v1/tenants/{first['id']}/allocate", json=allocation | {'bedId': 'phantom'}, headers=headers).status_code == 422
    assert client.post(f"/api/v1/tenants/{first['id']}/allocate", json=allocation, headers=headers).status_code == 200
    assert client.post(f"/api/v1/tenants/{second['id']}/allocate", json=allocation, headers=headers).status_code == 409
    assert client.post('/api/v1/inquiries', json=ROOM).status_code == 409
    assert client.get(f"/api/v1/tenants/{first['id']}/documents", headers=headers).json() == []
    assert client.get(f"/api/v1/tenants/{first['id']}/activity", headers=headers).json()
    owner = authorization(sign_in(client, 'owner@example.com'))
    assert client.get(f"/api/v1/tenants/{first['id']}", headers=owner).status_code == 403
    assert client.get('/api/v1/tenants', headers=owner).json()['total'] == 0
    tenant_headers = authorization(sign_in(client))
    assert client.get('/api/v1/tenants/me', headers=tenant_headers).json() is None
    assert client.get(f"/api/v1/tenants/{first['id']}", headers=tenant_headers).status_code == 403


def test_invitation_proof_and_self_service(setup, monkeypatch):
    client, factory, *_ = setup
    headers = admin(client)
    tenant = client.post('/api/v1/tenants', json=PROFILE | {'email': 'tenant@example.com'}, headers=headers).json()
    auth = authorization(sign_in(client))
    assert client.get('/api/v1/tenants/me', headers=auth).json() is None
    sent = []
    monkeypatch.setattr(tenants.email_service, 'send_tenant_link_email', lambda email, link: sent.append(link))
    response = client.post(f"/api/v1/tenants/{tenant['id']}/invite", headers=headers)
    assert response.status_code == 200, response.text
    token = unquote(sent[0].split('tenant-link=')[1])
    wrong = authorization(sign_in(client, 'owner@example.com'))
    assert client.post('/api/v1/tenants/accept-invitation', json={'token': token}, headers=wrong).status_code == 400
    result = client.post('/api/v1/tenants/accept-invitation', json={'token': token}, headers=auth)
    assert result.status_code == 200, result.text
    assert 'internalNotes' not in result.json()
    assert client.post('/api/v1/tenants/accept-invitation', json={'token': token}, headers=auth).status_code == 400
    assert 'internalNotes' not in client.get('/api/v1/tenants/me', headers=auth).json()
    assert client.patch('/api/v1/tenants/me', json={'phone': '9876543210', 'internalNotes': 'attack'}, headers=auth).status_code == 422
    assert client.patch('/api/v1/tenants/me', json={'phone': '9123456789'}, headers=auth).status_code == 200
    assert client.patch('/api/v1/tenants/me', json={'phone': '9123456789', 'emergencyEmail': 'invalid'}, headers=auth).status_code == 422


def test_provisioning_and_mail_failure(setup, monkeypatch):
    client, factory, sent, *_ = setup
    headers = admin(client)
    tenant = client.post('/api/v1/tenants', json=PROFILE, headers=headers).json()
    def fail(*args):
        raise RuntimeError('SMTP unavailable')
    monkeypatch.setattr(tenants.email_service, 'send_reset_email', fail)
    assert client.post(f"/api/v1/tenants/{tenant['id']}/invite", headers=headers).status_code == 503
    with factory() as db:
        assert db.scalar(select(User).where(User.email == PROFILE['email'])) is None
    monkeypatch.setattr(tenants.email_service, 'send_reset_email', lambda email, link: sent.append((email, link)))
    assert client.post(f"/api/v1/tenants/{tenant['id']}/invite", headers=headers).status_code == 200
    raw = sent[-1][1].split('#token=')[1]
    assert client.post('/api/v1/auth/reset-password', json={'token': raw, 'new_password': 'NewSecurePassword123!'}).status_code == 200
    auth = authorization(client.post('/api/v1/auth/login', json={'email': PROFILE['email'], 'password': 'NewSecurePassword123!'}))
    assert client.get('/api/v1/tenants/me', headers=auth).json()['id'] == tenant['id']
    privileged = client.post('/api/v1/tenants', json=PROFILE | {'email': 'admin@example.com'}, headers=headers).json()
    assert client.post(f"/api/v1/tenants/{privileged['id']}/invite", headers=headers).status_code == 409


def test_authenticated_conversion_links_only_tenant_identity(client):
    auth = authorization(sign_in(client))
    inquiry = client.post('/api/v1/inquiries', json=ROOM | {'email': 'tenant@example.com'}, headers=auth).json()
    headers = admin(client)
    for status in ('CONTACTED', 'SHORTLISTED', 'APPROVED'):
        assert client.patch(f"/api/v1/inquiries/{inquiry['id']}/status", json={'status': status}, headers=headers).status_code == 200
    result = client.post(f"/api/v1/inquiries/{inquiry['id']}/convert", json={}, headers=headers)
    assert result.status_code == 200
    assert client.get('/api/v1/tenants/me', headers=auth).json()['id'] == result.json()['tenant']['id']
    assert client.patch('/api/v1/tenants/me', json={'phone': '9876543210', 'email': 'different@example.com'}, headers=auth).status_code == 422


def test_guest_inquiries_require_controlled_identity_link(setup, monkeypatch):
    client, factory, *_ = setup
    headers = admin(client)
    guest = client.post('/api/v1/inquiries', json=ROOM).json()
    tenant = client.post('/api/v1/tenants', json=PROFILE, headers=headers).json()
    sent = []
    monkeypatch.setattr(tenants.email_service, 'send_reset_email', lambda email, link: sent.append(link))
    assert client.post(f"/api/v1/tenants/{tenant['id']}/invite", headers=headers).status_code == 200
    assert client.post('/api/v1/auth/reset-password', json={'token': sent[0].split('#token=')[1], 'new_password': 'SecureMailboxPassword123!'}).status_code == 200
    auth = authorization(client.post('/api/v1/auth/login', json={'email': PROFILE['email'], 'password': 'SecureMailboxPassword123!'}))
    assert client.get('/api/v1/inquiries/me', headers=auth).json()['items'][0]['id'] == guest['id']
    assert client.get(f"/api/v1/inquiries/{guest['id']}", headers=auth).status_code == 200
    assert client.post(f"/api/v1/inquiries/{guest['id']}/withdraw", headers=auth).status_code == 200


def test_concurrent_allocations_and_availability(setup):
    client, factory, *_ = setup
    headers = admin(client)
    ids = [client.post('/api/v1/tenants', json=PROFILE | {'email': f'person{n}@example.com'}, headers=headers).json()['id'] for n in range(2)]
    assert client.get('/api/v1/inquiries/availability').json()['room-sunrise-205']['bed-205-b'] is True

    def assign(tenant_id):
        from sqlalchemy.exc import IntegrityError
        with factory() as db:
            user = db.scalar(select(User).where(User.email == 'admin@example.com'))
            try:
                item = tenants.get(db, UUID(tenant_id), user, True)
                tenants.allocate(db, item, 'room-sunrise-205', 'bed-205-b', user)
                db.commit()
                return 200
            except (HTTPException, IntegrityError):
                db.rollback()
                return 409
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(assign, ids)) == [200, 409]
    assert client.get('/api/v1/inquiries/availability').json()['room-sunrise-205']['bed-205-b'] is False
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Occupancy)) == 1


def test_tenant_lifecycle_and_cross_property_allocation(setup):
    client, factory, *_ = setup
    headers = admin(client)
    item = client.post('/api/v1/tenants', json=PROFILE | {'status': 'PROSPECT'}, headers=headers).json()
    assert client.patch(f"/api/v1/tenants/{item['id']}/status", json={'status': 'NOTICE_PERIOD'}, headers=headers).status_code == 409
    assert client.patch(f"/api/v1/tenants/{item['id']}/status", json={'status': 'ONBOARDING'}, headers=headers).status_code == 200
    allocation = {'roomId': 'room-sunrise-205', 'bedId': 'bed-205-b', 'moveInDate': '2026-11-23'}
    assert client.post(f"/api/v1/tenants/{item['id']}/allocate", json=allocation, headers=headers).status_code == 200
    for target in ('ACTIVE', 'NOTICE_PERIOD', 'ACTIVE', 'INACTIVE'):
        assert client.patch(f"/api/v1/tenants/{item['id']}/status", json={'status': target}, headers=headers).status_code == 200
    assert client.get(f"/api/v1/tenants/{item['id']}", headers=headers).json()['bedId'] is None
    assert client.patch(f"/api/v1/tenants/{item['id']}/status", json={'status': 'ACTIVE'}, headers=headers).status_code == 409
    with factory() as db:
        owner = db.scalar(select(User).where(User.email == 'owner@example.com'))
        db.get(Property, 'prop-sunrise').owner_user_id = owner.id
        db.add(PropertyAssignment(user_id=owner.id, property_id='prop-sunrise'))
        db.commit()
    auth = authorization(sign_in(client, 'owner@example.com'))
    assert client.get(f"/api/v1/tenants/{item['id']}", headers=auth).status_code == 200
    assert client.patch(f"/api/v1/tenants/{item['id']}", json={'propertyId': 'prop-oasis'}, headers=auth).status_code in (403, 422)
    assert client.get(f"/api/v1/tenants/{item['id']}", headers=headers).json()['propertyId'] == 'prop-sunrise'


def test_body_limits_pagination_and_production_boundary(client, monkeypatch):
    assert client.post('/api/v1/inquiries/general', content='x' * 17000, headers={'Content-Type': 'application/json'}).status_code == 413
    headers = admin(client)
    for n in range(3):
        assert client.post('/api/v1/tenants', json=PROFILE | {'email': f'page{n}@example.com'}, headers=headers).status_code == 201
    first = client.get('/api/v1/tenants?limit=2', headers=headers).json()
    second = client.get('/api/v1/tenants?limit=2&offset=2', headers=headers).json()
    assert first['total'] == 3 and len(first['items']) == 2 and len(second['items']) == 1
    assert client.get('/api/v1/tenants?limit=1000', headers=headers).status_code == 422
    # Canonical database inventory is available without a development catalog.
    assert client.post('/api/v1/inquiries', json=ROOM).status_code == 201
    assert client.get('/api/v1/inquiries/availability').status_code == 200
    # General leads are independent of inventory and never create accounts/roles.
    assert client.post('/api/v1/inquiries/general', json={'name': 'General Lead', 'email': 'general@example.com', 'inquiryType': 'staff'}).status_code == 201
