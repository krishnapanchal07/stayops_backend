from app.modules.properties.models import Property
"""Staff tests use only conftest's disposable, migrated PostgreSQL cluster."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import PASSWORD, authorization, sign_in
from app.core.security import now, password_hasher
from app.modules.auth.models import AuthSession, PasswordResetToken, PropertyAssignment, User
from app.modules.staff import service
from app.modules.staff.models import StaffLinkInvitation, StaffProfile
from app.modules.staff.schemas import StaffCreate

PROFILE = {'firstName': 'Sara', 'lastName': 'Patel', 'email': 'sara@example.com', 'phone': '9876543210',
    'role': 'WARDEN', 'department': 'OPERATIONS', 'designation': 'Resident Warden', 'propertyIds': ['prop-sunrise'],
    'joiningDate': '2026-11-24', 'status': 'ACTIVE', 'profileImage': '', 'addressLine1': 'Staff quarters',
    'city': 'Ahmedabad', 'state': 'Gujarat', 'emergencyName': 'Family Contact', 'emergencyPhone': '9123456789'}


@pytest.fixture
def staff_setup(setup, monkeypatch):
    sent = []
    monkeypatch.setattr(service.email_service, 'send_staff_setup_email', lambda email, link: sent.append(('setup', email, link)))
    monkeypatch.setattr(service.email_service, 'send_staff_link_email', lambda email, link: sent.append(('link', email, link)))
    client, factory, *_ = setup
    headers = authorization(sign_in(client, 'admin@example.com'))
    return client, factory, headers, sent


def create(context, **changes):
    client, _, headers, _ = context
    response = client.post('/api/v1/staff', json=PROFILE | changes, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def setup_password(context, email='sara@example.com'):
    client, _, _, sent = context
    raw = next(item[2].split('#token=')[1] for item in reversed(sent) if item[1] == email)
    result = client.post('/api/v1/auth/reset-password', json={'token': raw, 'new_password': PASSWORD})
    assert result.status_code == 200, result.text
    return raw


def test_create_setup_login_and_read(staff_setup):
    client, factory, headers, sent = staff_setup
    item = create(staff_setup, email=' SARA@EXAMPLE.COM ')
    assert item['employeeCode'] == 'STF-1001'
    assert item['propertyNames'] == ['Sunrise PG'] and not item['accountLinkPending']
    assert sent[0][0:2] == ('setup', 'sara@example.com')
    assert 'password' not in str(item).lower() and '#token=' not in str(item)
    assert sign_in(client, 'sara@example.com').status_code == 401
    raw = setup_password(staff_setup)
    login = sign_in(client, 'sara@example.com')
    assert login.status_code == 200
    assert login.json()['role'] == 'WARDEN' and login.json()['user']['propertyIds'] == ['prop-sunrise']
    assert client.post('/api/v1/auth/reset-password', json={'token': raw, 'new_password': PASSWORD}).status_code == 400
    assert client.get(f"/api/v1/staff/{item['id']}", headers=headers).json()['id'] == item['id']
    assert client.get('/api/v1/staff/summary', headers=headers).json() == {'total': 1, 'active': 1, 'inactive': 0, 'onLeave': 0}
    with factory() as db:
        account = db.get(User, UUID(item['userId']))
        assert password_hasher.verify(PASSWORD, account.password_hash)
        assert db.scalar(select(func.count()).select_from(StaffProfile)) == 1


@pytest.mark.parametrize('patch', [
    {'role': 'SUPER_ADMIN'}, {'role': 'OWNER'}, {'role': 'TENANT'}, {'role': 'INVALID'},
    {'phone': '123'}, {'emergencyPhone': 'abc'}, {'firstName': ' '}, {'department': 'INVALID'},
    {'designation': 'x' * 121}, {'propertyIds': []}, {'propertyIds': ['prop-sunrise', 'prop-sunrise']},
    {'joiningDate': 'not-a-date'}, {'password': 'bad-password'}, {'userId': 'spoof'}, {'profileImage': 'javascript:alert(1)'},
])
def test_invalid_inputs_and_escalation(staff_setup, patch):
    client, factory, headers, sent = staff_setup
    assert client.post('/api/v1/staff', json=PROFILE | patch, headers=headers).status_code == 422
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StaffProfile)) == 0
    assert not sent


def test_duplicate_email_profile_and_incompatible_accounts(staff_setup):
    client, factory, headers, _ = staff_setup
    item = create(staff_setup)
    assert client.post('/api/v1/staff', json=PROFILE, headers=headers).status_code == 409
    for email in ('admin@example.com', 'owner@example.com', 'tenant@example.com', 'inactive@example.com'):
        assert client.post('/api/v1/staff', json=PROFILE | {'email': email}, headers=headers).status_code == 409
    assert client.post('/api/v1/staff', json=PROFILE | {'email': 'different@example.com', 'existingUserId': item['userId']}, headers=headers).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StaffProfile)) == 1


def test_existing_account_requires_explicit_mailbox_link(staff_setup):
    client, factory, headers, sent = staff_setup
    with factory() as db:
        account = User(full_name='Existing Warden', email='existing@example.com', role_id='WARDEN', password_hash=password_hasher.hash(PASSWORD))
        db.add(account)
        db.flush()
        db.add(PropertyAssignment(user_id=account.id, property_id='prop-greenview'))
        account_id, original_hash = str(account.id), account.password_hash
        db.commit()
    candidate = client.get('/api/v1/staff/account-candidate?email=existing@example.com', headers=headers)
    assert candidate.status_code == 200 and candidate.json()['id'] == account_id
    assert client.post('/api/v1/staff', json=PROFILE | {'email': 'existing@example.com'}, headers=headers).status_code == 409
    item = create(staff_setup, email='existing@example.com', existingUserId=account_id)
    assert item['accountLinkPending'] and item['fullName'] == 'Existing Warden'
    assert sent[-1][0] == 'link'
    assert client.patch(f"/api/v1/staff/{item['id']}/status", json={'status': 'INACTIVE'}, headers=headers).status_code == 409
    with factory() as db:
        account = db.get(User, UUID(account_id))
        assert account.password_hash == original_hash and account.is_active
        assert [a.property_id for a in account.property_assignments] == ['prop-greenview']
    token = sent[-1][2].split('#staff-link=')[1]
    wrong = authorization(sign_in(client))
    assert client.post('/api/v1/staff/accept-invitation', json={'token': token}, headers=wrong).status_code == 400
    existing = authorization(sign_in(client, 'existing@example.com'))
    assert client.post('/api/v1/staff/accept-invitation', json={'token': token}, headers=existing).status_code == 200
    assert client.get('/api/v1/auth/me', headers=existing).status_code == 401
    existing = authorization(sign_in(client, 'existing@example.com'))
    assert client.post('/api/v1/staff/accept-invitation', json={'token': token}, headers=existing).status_code == 400
    assert sign_in(client, 'existing@example.com').json()['user']['propertyIds'] == ['prop-sunrise']
    with factory() as db:
        assert db.get(User, UUID(account_id)).password_hash == original_hash


def test_lifecycle_revokes_sessions_and_on_leave_preserves_access(staff_setup):
    client, factory, headers, _ = staff_setup
    item = create(staff_setup)
    setup_password(staff_setup)
    login = sign_in(client, 'sara@example.com')
    token = authorization(login)
    refresh_cookie = client.cookies.get('stayops_refresh')
    assert client.patch(f"/api/v1/staff/{item['id']}/status", json={'status': 'ON_LEAVE'}, headers=headers).status_code == 200
    assert client.get('/api/v1/auth/me', headers=token).status_code == 200
    assert sign_in(client, 'sara@example.com').status_code == 200
    assert client.patch(f"/api/v1/staff/{item['id']}/status", json={'status': 'INACTIVE'}, headers=headers).status_code == 200
    assert client.get('/api/v1/auth/me', headers=token).status_code == 401
    assert sign_in(client, 'sara@example.com').status_code == 401
    client.cookies.set('stayops_refresh', refresh_cookie)
    assert client.post('/api/v1/auth/refresh').status_code == 401
    assert client.patch(f"/api/v1/staff/{item['id']}/status", json={'status': 'ACTIVE'}, headers=headers).status_code == 200
    assert sign_in(client, 'sara@example.com').status_code == 200
    assert client.get('/api/v1/auth/me', headers=token).status_code == 401
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StaffProfile)) == 1


def test_edit_assignments_atomicity_and_privileged_fields(staff_setup):
    client, factory, headers, _ = staff_setup
    item = create(staff_setup)
    setup_password(staff_setup)
    auth = authorization(sign_in(client, 'sara@example.com'))
    result = client.patch(f"/api/v1/staff/{item['id']}", json={'firstName': 'Changed', 'designation': 'Senior Warden', 'propertyIds': ['prop-greenview'], 'status': 'ON_LEAVE'}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['fullName'] == 'Changed Patel' and result.json()['propertyIds'] == ['prop-greenview']
    assert client.get('/api/v1/auth/me', headers=auth).status_code == 401
    for patch in ({'email': 'new@example.com'}, {'role': 'SUPER_ADMIN'}, {'userId': item['userId']}, {'is_active': True}):
        assert client.patch(f"/api/v1/staff/{item['id']}", json=patch, headers=headers).status_code == 422
    assert client.patch(f"/api/v1/staff/{item['id']}", json={'designation': 'Should roll back', 'propertyIds': ['phantom']}, headers=headers).status_code == 422
    assert client.get(f"/api/v1/staff/{item['id']}", headers=headers).json()['designation'] == 'Senior Warden'
    assert client.patch(f"/api/v1/staff/{item['id']}/assignments", json={'propertyIds': ['prop-sunrise']}, headers=headers).status_code == 200


def test_permissions_and_complete_property_scope(staff_setup):
    client, factory, headers, _ = staff_setup
    item = create(staff_setup)
    assert client.get('/api/v1/staff').status_code == 401
    tenant = authorization(sign_in(client))
    assert client.get('/api/v1/staff', headers=tenant).status_code == 403
    owner = authorization(sign_in(client, 'owner@example.com'))
    assert client.get('/api/v1/staff', headers=owner).json()['total'] == 0
    assert client.get(f"/api/v1/staff/{item['id']}", headers=owner).status_code == 403
    assert client.post('/api/v1/staff', json=PROFILE, headers=owner).status_code == 403
    with factory() as db:
        account = db.scalar(select(User).where(User.email == 'owner@example.com'))
        db.get(Property, 'prop-sunrise').owner_user_id = account.id
        db.add(PropertyAssignment(user_id=account.id, property_id='prop-sunrise'))
        db.commit()
    assert client.get('/api/v1/staff', headers=owner).json()['total'] == 1
    assert client.patch(f"/api/v1/staff/{item['id']}", json={'designation': 'Owner edited'}, headers=owner).status_code == 200
    assert client.patch(f"/api/v1/staff/{item['id']}/assignments", json={'propertyIds': ['prop-greenview']}, headers=owner).status_code == 403
    assert client.post(f"/api/v1/staff/{item['id']}/resend-invite", headers=owner).status_code == 403
    assert client.get('/api/v1/staff/properties', headers=owner).json() == [{'id': 'prop-sunrise', 'name': 'Sunrise PG'}]
    assert client.patch(f"/api/v1/staff/{item['id']}/assignments", json={'propertyIds': ['prop-sunrise', 'prop-greenview']}, headers=headers).status_code == 200
    assert client.get('/api/v1/staff', headers=owner).json()['total'] == 0
    assert client.get(f"/api/v1/staff/{item['id']}", headers=owner).status_code == 403


def test_invitation_expiry_resend_and_delivery_rollback(staff_setup, monkeypatch):
    client, factory, headers, sent = staff_setup
    def fail(*args):
        raise RuntimeError('SMTP failed')
    original = service.email_service.send_staff_setup_email
    monkeypatch.setattr(service.email_service, 'send_staff_setup_email', fail)
    assert client.post('/api/v1/staff', json=PROFILE, headers=headers).status_code == 503
    with factory() as db:
        assert db.scalar(select(User).where(User.email == PROFILE['email'])) is None
        assert db.scalar(select(func.count()).select_from(StaffProfile)) == 0
    monkeypatch.setattr(service.email_service, 'send_staff_setup_email', original)
    item = create(staff_setup)
    expired = sent[-1][2].split('#token=')[1]
    with factory() as db:
        token = db.scalar(select(PasswordResetToken).where(PasswordResetToken.user_id == UUID(item['userId'])))
        token.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post('/api/v1/auth/reset-password', json={'token': expired, 'new_password': PASSWORD}).status_code == 400
    assert client.post(f"/api/v1/staff/{item['id']}/resend-invite", headers=headers).status_code == 200
    setup_password(staff_setup)
    assert sign_in(client, 'sara@example.com').status_code == 200


def test_concurrent_creates_and_employee_codes(staff_setup):
    _, factory, _, _ = staff_setup
    def provision(email):
        with factory() as db:
            user = db.scalar(select(User).where(User.email == 'admin@example.com'))
            try:
                item = service.create(db, StaffCreate.model_validate(PROFILE | {'email': email}), user)
                return item.employee_code
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(provision, ['concurrent@example.com'] * 2))
    assert sum(isinstance(value, str) for value in results) == 1 and 409 in results
    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(provision, ['first@example.com', 'second@example.com']))
    assert len(set(codes)) == 2 and all(code.startswith('STF-') for code in codes)


def test_filters_pagination_sorting_and_real_summary(staff_setup):
    client, _, headers, _ = staff_setup
    create(staff_setup)
    create(staff_setup, email='security@example.com', firstName='Zara', role='SECURITY', department='SECURITY', status='ON_LEAVE', propertyIds=['prop-greenview'])
    create(staff_setup, email='inactive-staff@example.com', status='INACTIVE')
    for query in ('role=SECURITY', 'department=SECURITY', 'status=ON_LEAVE', 'query=Zara', 'propertyId=prop-greenview'):
        assert client.get('/api/v1/staff?' + query, headers=headers).json()['total'] == 1
    assert client.get('/api/v1/staff?limit=1&sortBy=fullName&sortDirection=desc', headers=headers).json()['items'][0]['firstName'] == 'Zara'
    assert len(client.get('/api/v1/staff?limit=2&offset=2', headers=headers).json()['items']) == 1
    assert client.get('/api/v1/staff?sortBy=password_hash', headers=headers).status_code == 422
    assert client.get('/api/v1/staff/summary', headers=headers).json() == {'total': 3, 'active': 1, 'inactive': 1, 'onLeave': 1}


@pytest.mark.parametrize('role,department', [('WARDEN', 'OPERATIONS'), ('ACCOUNTANT', 'FINANCE'), ('SECURITY', 'SECURITY'), ('MAINTENANCE', 'MAINTENANCE')])
def test_all_supported_staff_roles_log_in(staff_setup, role, department):
    client, _, _, _ = staff_setup
    create(staff_setup, role=role, department=department)
    setup_password(staff_setup)
    login = sign_in(client, 'sara@example.com')
    assert login.status_code == 200 and login.json()['role'] == role
    auth = authorization(login)
    assert client.get('/api/v1/staff', headers=auth).status_code == 403
    assert client.patch('/api/v1/auth/me', json={'name': 'Self', 'role': 'SUPER_ADMIN'}, headers=auth).status_code == 422


def test_pending_link_expiry_resend_and_email_failure_preserves_account(staff_setup, monkeypatch):
    client, factory, headers, sent = staff_setup
    with factory() as db:
        account = User(full_name='Existing Staff', email='existing@example.com', role_id='WARDEN', password_hash=password_hasher.hash(PASSWORD))
        db.add(account)
        db.commit()
        account_id, original_hash = str(account.id), account.password_hash
    item = create(staff_setup, email='existing@example.com', existingUserId=account_id)
    old = sent[-1][2].split('#staff-link=')[1]
    with factory() as db:
        invitation = db.scalar(select(StaffLinkInvitation).where(StaffLinkInvitation.staff_id == UUID(item['id'])))
        invitation.expires_at = now() - timedelta(seconds=1)
        db.commit()
    auth = authorization(sign_in(client, 'existing@example.com'))
    assert client.post('/api/v1/staff/accept-invitation', json={'token': old}, headers=auth).status_code == 400
    assert client.post(f"/api/v1/staff/{item['id']}/resend-invite", headers=headers).status_code == 200
    valid = sent[-1][2].split('#staff-link=')[1]
    sender = service.email_service.send_staff_link_email
    def fail(*args):
        raise RuntimeError('SMTP unavailable')
    monkeypatch.setattr(service.email_service, 'send_staff_link_email', fail)
    assert client.post(f"/api/v1/staff/{item['id']}/resend-invite", headers=headers).status_code == 503
    # Failed resend rolls back token rotation, preserving the previously delivered link.
    assert client.post('/api/v1/staff/accept-invitation', json={'token': valid}, headers=auth).status_code == 200
    with factory() as db:
        assert db.get(User, UUID(account_id)).password_hash == original_hash


def test_on_leave_preserves_existing_disabled_login(staff_setup):
    client, factory, headers, _ = staff_setup
    item = create(staff_setup, status='INACTIVE')
    assert client.patch(f"/api/v1/staff/{item['id']}/status", json={'status': 'ON_LEAVE'}, headers=headers).status_code == 200
    with factory() as db:
        assert not db.get(User, UUID(item['userId'])).is_active
    assert client.post(f"/api/v1/staff/{item['id']}/resend-invite", headers=headers).status_code == 409


def test_additive_migration_preserves_previous_phase_records(setup):
    from alembic import command
    from sqlalchemy import inspect, text
    client, _, _, engine, config = setup
    headers = authorization(sign_in(client, 'admin@example.com'))
    assert client.post('/api/v1/inquiries/general', json={'name': 'Existing Lead', 'email': 'lead@example.com', 'inquiryType': 'owner'}).status_code == 201
    assert client.post('/api/v1/tenants', json={'firstName': 'Existing', 'lastName': 'Tenant', 'email': 'kept@example.com', 'phone': '9123456789'}, headers=headers).status_code == 201
    with engine.begin() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, '0002_inquiries_tenants')
        protected = [name for name in inspect(connection).get_table_names()
                     if name not in {'alembic_version', 'permissions', 'role_permissions'}]
        before = {name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() for name in protected}
        command.upgrade(config, 'head')
        after = {name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() for name in protected}
        assert before == after
        assert connection.scalar(text("SELECT count(*) FROM permissions WHERE id = 'FINANCE_REPORT_VIEW'")) == 1
        assert connection.scalar(text("SELECT count(*) FROM permissions WHERE id ~ '^(VISITOR|LEAVE|NOTICE|DOCUMENT|SUPPORT|AUDIT)_'")) == 0
        assert connection.scalar(text("SELECT count(*) FROM roles WHERE id='SECURITY'")) == 1
        assert 'staff_profiles' in inspect(connection).get_table_names()


def test_single_name_existing_identity_can_link_and_be_managed(staff_setup):
    client, factory, headers, sent = staff_setup
    with factory() as db:
        user = User(full_name='Suresh', email='single@example.com', role_id='WARDEN', password_hash=password_hasher.hash(PASSWORD))
        db.add(user)
        db.commit()
        user_id = str(user.id)
    item = create(staff_setup, firstName='Suresh', lastName='', email='single@example.com', existingUserId=user_id)
    token = sent[-1][2].split('#staff-link=')[1]
    auth = authorization(sign_in(client, 'single@example.com'))
    assert client.post('/api/v1/staff/accept-invitation', json={'token': token}, headers=auth).status_code == 200
    updated = client.patch(f"/api/v1/staff/{item['id']}", json={'designation': 'Senior Warden'}, headers=headers)
    assert updated.status_code == 200 and updated.json()['fullName'] == 'Suresh'
