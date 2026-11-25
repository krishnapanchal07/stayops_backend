from app.modules.properties.models import Property
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import HTTPException
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import digest, now, password_hasher
from app.db.base import Base
from app.modules.auth.dependencies import ensure_property_access, ensure_record_owner
from app.modules.auth.models import AuthSession, PasswordResetToken, PropertyAssignment, RefreshToken, Role, User
from app.modules.auth.seed import seed_roles
from conftest import HEADERS, PASSWORD, authorization, sign_in

BASE = "/api/v1/auth"


def reset_token(sent):
    return parse_qs(urlsplit(sent[-1][1]).fragment)["token"][0]


def test_health_and_migrations(setup):
    client, factory, _, engine, config = setup
    assert client.get("/api/v1/health").json()["database"] == "ok"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version() ")).scalar().startswith("PostgreSQL 18")
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0005_inventory"
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Role)) == 7
        assert db.scalar(select(User).where(User.email == "tenant@example.com")).role.id == "TENANT"
        seed_roles(db)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Role)) == 7


def test_migration_round_trip(setup):
    _, _, _, engine, config = setup
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
        assert "users" not in inspect(connection).get_table_names()
        command.upgrade(config, "head")
        assert "users" in inspect(connection).get_table_names()


def test_seed_contract_matches_frontend():
    root = Path(__file__).resolve().parents[2]
    for name in ("roles.json", "permissions.json"):
        assert json.loads((root / "Fronted/src/mock" / name).read_text()) == json.loads((root / "Backend/app/modules/auth" / name).read_text())


def test_unique_normalized_email(setup):
    with setup[1]() as db:
        db.add(User(full_name="Duplicate", email="tenant@example.com", role_id="TENANT", password_hash="unused"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(User(full_name="Uppercase", email="UPPER@example.com", role_id="TENANT", password_hash="unused"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_login_tokens_and_me(setup):
    client, factory, *_ = setup
    response = sign_in(client, " TENANT@EXAMPLE.COM ")
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == data["user"]["roleId"] == "TENANT"
    assert "PROPERTY_DELETE" not in data["permissions"]
    assert "password" not in response.text
    claims = jwt.decode(data["access_token"], get_settings().jwt_secret_key.get_secret_value(), algorithms=["HS256"], audience="stayops-api", issuer="stayops")
    assert claims["type"] == "access" and claims["sid"] and claims["exp"] > claims["iat"]
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    assert client.get(BASE + "/me", headers=authorization(response)).status_code == 200
    with factory() as db:
        user = db.scalar(select(User).where(User.email == "tenant@example.com"))
        assert user.password_hash.startswith("$argon2id$")
        assert password_hasher.verify(PASSWORD, user.password_hash)
        token = db.scalar(select(RefreshToken))
        assert token.token_digest == digest(client.cookies.get("stayops_refresh"))


@pytest.mark.parametrize("email,password", [("tenant@example.com", "wrong"), ("missing@example.com", PASSWORD), ("inactive@example.com", PASSWORD)])
def test_invalid_credentials(client, email, password):
    response = client.post(BASE + "/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Incorrect email or password."


@pytest.mark.parametrize("payload", [{}, {"email": "invalid", "password": "secret"}, {"email": "tenant@example.com", "password": PASSWORD, "role": "SUPER_ADMIN"}])
def test_malformed_or_escalating_login(client, payload):
    response = client.post(BASE + "/login", json=payload)
    assert response.status_code == 422
    assert PASSWORD not in response.text and "secret" not in response.text


@pytest.mark.parametrize("token", [None, "garbage"])
def test_missing_invalid_bearer(client, token):
    headers = {"Authorization": "Bearer " + token} if token else {}
    assert client.get(BASE + "/me", headers=headers).status_code == 401


def test_expired_and_wrong_type_jwt(client):
    response = sign_in(client)
    settings = get_settings()
    claims = jwt.decode(response.json()["access_token"], settings.jwt_secret_key.get_secret_value(), algorithms=["HS256"], audience="stayops-api")
    for patch in ({"exp": int((now() - timedelta(seconds=1)).timestamp())}, {"type": "refresh"}, {"aud": "wrong"}):
        invalid = jwt.encode(claims | patch, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")
        assert client.get(BASE + "/me", headers={"Authorization": "Bearer " + invalid}).status_code == 401


def test_role_permission_and_privilege_escalation(client):
    tenant = authorization(sign_in(client))
    assert client.get("/test/admin", headers=tenant).status_code == 403
    assert client.get("/test/permission", headers=tenant).status_code == 403
    payload = {"full_name": "Attacker", "email": "attacker@example.com", "password": PASSWORD, "role": "SUPER_ADMIN"}
    assert client.post(BASE + "/users", json=payload).status_code == 401
    assert client.post(BASE + "/users", json=payload, headers=tenant).status_code == 403
    assert client.patch(BASE + "/me", json={"name": "Attacker", "role": "SUPER_ADMIN"}, headers=tenant).status_code == 422
    admin = authorization(sign_in(client, "admin@example.com"))
    assert client.get("/test/admin", headers=admin).status_code == 200
    assert client.get("/test/permission", headers=admin).status_code == 200
    assert client.post(BASE + "/users", json=payload, headers=admin).status_code == 201
    assert client.post(BASE + "/users", json=payload, headers=admin).status_code == 409


def test_resource_scopes(setup):
    with setup[1]() as db:
        owner = db.scalar(select(User).where(User.email == "owner@example.com"))
        with pytest.raises(HTTPException):
            ensure_property_access(owner, "another-property")
        with pytest.raises(HTTPException):
            ensure_record_owner(owner, uuid4())
        db.get(Property, "prop-sunrise").owner_user_id = owner.id
        owner.property_assignments.append(PropertyAssignment(property_id="prop-sunrise"))
        db.flush()
        ensure_property_access(owner, "prop-sunrise")
        ensure_record_owner(owner, owner.id)


def test_rotation_and_replay_revokes_family(client):
    initial = sign_in(client)
    previous = client.cookies.get("stayops_refresh")
    rotated = client.post(BASE + "/refresh")
    replacement = client.cookies.get("stayops_refresh")
    assert rotated.status_code == 200 and replacement != previous
    client.cookies.set("stayops_refresh", previous, domain="testserver.local", path=BASE)
    assert client.post(BASE + "/refresh").status_code == 401
    assert client.get(BASE + "/me", headers=authorization(initial)).status_code == 401
    client.cookies.set("stayops_refresh", replacement, domain="testserver.local", path=BASE)
    assert client.post(BASE + "/refresh").status_code == 401


def test_expired_refresh(setup):
    client, factory, *_ = setup
    sign_in(client)
    with factory() as db:
        session = db.scalar(select(AuthSession))
        session.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post(BASE + "/refresh").status_code == 401


def test_logout_revokes_access_and_refresh(client):
    headers = authorization(sign_in(client))
    raw = client.cookies.get("stayops_refresh")
    assert client.post(BASE + "/logout").status_code == 200
    assert client.get(BASE + "/me", headers=headers).status_code == 401
    client.cookies.set("stayops_refresh", raw, domain="testserver.local", path=BASE)
    assert client.post(BASE + "/refresh").status_code == 401
    assert client.post(BASE + "/logout").status_code == 200


def test_inactive_user_existing_session(setup):
    client, factory, *_ = setup
    headers = authorization(sign_in(client))
    with factory() as db:
        db.scalar(select(User).where(User.email == "tenant@example.com")).is_active = False
        db.commit()
    assert client.get(BASE + "/me", headers=headers).status_code == 401
    assert client.post(BASE + "/refresh").status_code == 401


def test_forgot_generic_and_digest_only(setup):
    client, factory, sent, *_ = setup
    existing = client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"})
    unknown = client.post(BASE + "/forgot-password", json={"email": "unknown@example.com"})
    inactive = client.post(BASE + "/forgot-password", json={"email": "inactive@example.com"})
    assert existing.status_code == unknown.status_code == inactive.status_code == 202
    assert existing.json() == unknown.json() == inactive.json()
    assert len(sent) == 1 and sent[0][0] == "tenant@example.com"
    token = reset_token(sent)
    assert urlsplit(sent[0][1]).netloc == "localhost:5173" and not urlsplit(sent[0][1]).query
    with factory() as db:
        stored = db.scalar(select(PasswordResetToken))
        assert stored.token_digest == digest(token) and stored.token_digest != token


def test_reset_success_reuse_and_session_revocation(setup):
    client, factory, sent, *_ = setup
    headers = authorization(sign_in(client))
    refresh = client.cookies.get("stayops_refresh")
    client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"})
    token = reset_token(sent)
    payload = {"token": token, "new_password": "New-password-45678"}
    assert client.post(BASE + "/reset-password", json=payload).status_code == 200
    assert client.post(BASE + "/reset-password", json=payload).status_code == 400
    assert client.get(BASE + "/me", headers=headers).status_code == 401
    client.cookies.set("stayops_refresh", refresh, domain="testserver.local", path=BASE)
    assert client.post(BASE + "/refresh").status_code == 401
    assert sign_in(client).status_code == 401
    assert client.post(BASE + "/login", json={"email": "tenant@example.com", "password": payload["new_password"]}).status_code == 200


def test_expired_invalid_and_superseded_reset(setup):
    client, factory, sent, *_ = setup
    payload = {"token": "invalid-token-" * 4, "new_password": "New-password-45678"}
    assert client.post(BASE + "/reset-password", json=payload).status_code == 400
    for _ in range(2):
        client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"})
    old = parse_qs(urlsplit(sent[0][1]).fragment)["token"][0]
    assert client.post(BASE + "/reset-password", json=payload | {"token": old}).status_code == 400
    with factory() as db:
        active = db.scalar(select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None)))
        active.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post(BASE + "/reset-password", json=payload | {"token": reset_token(sent)}).status_code == 400


def test_smtp_failure_rolls_back(setup, monkeypatch):
    client, factory, sent, *_ = setup
    client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"})
    previous = reset_token(sent)
    def failure(*args):
        raise OSError("SMTP unreachable")
    monkeypatch.setattr("app.modules.auth.service.email_service.send_reset_email", failure)
    assert client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"}).status_code == 202
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PasswordResetToken)) == 1
    assert client.post(BASE + "/reset-password", json={"token": previous, "new_password": "Another-password-123"}).status_code == 200


def test_password_policy_and_change(client):
    headers = authorization(sign_in(client))
    for password in ("short1", "onlyletterslongenough", "1" * 14):
        assert client.post(BASE + "/change-password", headers=headers, json={"current_password": PASSWORD, "new_password": password}).status_code == 422
    assert client.post(BASE + "/change-password", headers=headers, json={"current_password": "wrong", "new_password": "New-password-12345"}).status_code == 400
    assert client.post(BASE + "/change-password", headers=headers, json={"current_password": PASSWORD, "new_password": "New-password-12345"}).status_code == 200
    assert client.get(BASE + "/me", headers=headers).status_code == 401


def test_profile_cannot_change_identity(client):
    headers = authorization(sign_in(client))
    assert client.patch(BASE + "/me", headers=headers, json={"name": "New Name"}).json()["user"]["name"] == "New Name"
    assert client.patch(BASE + "/me", headers=headers, json={"name": "New Name", "email": "attacker@example.com"}).status_code == 422


def test_remember_cookie(client):
    response = sign_in(client, remember=False)
    assert "Max-Age" not in response.headers["set-cookie"]
    assert "Max-Age" not in client.post(BASE + "/refresh").headers["set-cookie"]


@pytest.mark.parametrize("endpoint", ["login", "refresh", "logout", "forgot-password", "reset-password"])
def test_csrf_rejects_untrusted_origin(client, endpoint):
    response = client.post(BASE + "/" + endpoint, headers={"Origin": "https://evil.example"}, json={"email": "tenant@example.com", "password": PASSWORD})
    assert response.status_code == 403


def test_rate_limit(client):
    for _ in range(10):
        response = client.post(BASE + "/login", json={"email": "unknown@example.com", "password": "incorrect"})
        assert response.status_code == 401
    assert client.post(BASE + "/login", json={"email": "unknown@example.com", "password": "incorrect"}).status_code == 429


def test_cors_and_security_headers(client):
    response = client.options(BASE + "/refresh", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-StayOps-CSRF"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"
    blocked = client.options(BASE + "/refresh", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert blocked.status_code == 400
    assert client.get(BASE + "/me").headers["cache-control"] == "no-store"


def test_concurrent_refresh_cannot_reuse_same_token(setup):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.auth import service
    client, factory, *_ = setup
    sign_in(client)
    raw = client.cookies.get("stayops_refresh")
    def rotate():
        with factory() as db:
            try:
                service.refresh(db, raw)
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: rotate(), range(2))) == [200, 401]
    with factory() as db:
        assert db.scalar(select(AuthSession)).revoked_at is not None


def test_concurrent_password_reset_single_use(setup):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.auth import service
    client, factory, sent, *_ = setup
    client.post(BASE + "/forgot-password", json={"email": "tenant@example.com"})
    raw = reset_token(sent)
    def reset():
        with factory() as db:
            try:
                service.reset_password(db, raw, "New-password-12345")
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: reset(), range(2))) == [200, 400]


def test_health_database_failure_is_sanitized(setup, monkeypatch):
    from app.db.session import get_db
    from sqlalchemy.exc import OperationalError
    class Unavailable:
        def execute(self, *_):
            raise OperationalError("SELECT 1", {}, Exception("private-database-details"))
    setup[0].app.dependency_overrides[get_db] = lambda: Unavailable()
    response = setup[0].get("/api/v1/health")
    assert response.status_code == 503
    assert "private" not in response.text


def test_profile_image_validation(client):
    import base64
    headers = authorization(sign_in(client))
    data = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nexample").decode()
    assert client.patch(BASE + "/me", headers=headers, json={"name": "Tenant", "avatar": data}).status_code == 200
    for avatar in ("javascript:alert(1)", "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,aW52YWxpZA=="):
        assert client.patch(BASE + "/me", headers=headers, json={"name": "Tenant", "avatar": avatar}).status_code == 422


def test_csrf_header_and_origin_required(client):
    from fastapi.testclient import TestClient
    with TestClient(client.app) as bare:
        for headers in ({}, {"Origin": "http://localhost:5173"}, {"X-StayOps-CSRF": "1"}):
            assert bare.post(BASE + "/logout", headers=headers).status_code == 403
