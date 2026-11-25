from uuid import UUID

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import decode_access, now
from app.db.session import get_db
from app.modules.auth.models import AuthSession, User

bearer = HTTPBearer(auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(401, "Authentication required or session expired.", headers={"WWW-Authenticate": "Bearer"})


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not credentials:
        raise unauthorized()
    try:
        claims = decode_access(credentials.credentials)
        if claims["type"] != "access":
            raise ValueError("Invalid token type")
        user_id, session_id = UUID(claims["sub"]), UUID(claims["sid"])
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise unauthorized() from None
    session = db.get(AuthSession, session_id)
    if not session or session.user_id != user_id or session.revoked_at or session.expires_at <= now():
        raise unauthorized()
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise unauthorized()
    return user


get_active_user = get_current_user


def get_optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User | None:
    # An invalid supplied token is never silently downgraded to a guest request.
    return get_current_user(credentials, db) if credentials else None


def require_role(*roles: str):
    def dependency(user: User = Depends(get_active_user)) -> User:
        if user.role_id not in roles:
            raise HTTPException(403, "You do not have access to this operation.")
        return user
    return dependency


def require_permission(*permissions: str):
    def dependency(user: User = Depends(get_active_user)) -> User:
        actual = {permission.id for permission in user.role.permissions}
        if not set(permissions).issubset(actual):
            raise HTTPException(403, "You do not have permission for this operation.")
        return user
    return dependency


def ensure_property_access(user: User, property_id: str) -> None:
    """Call after permission checks, with property ID loaded from the target record."""
    if property_id not in trusted_property_ids(user) and user.role_id != "SUPER_ADMIN":
        raise HTTPException(403, "You do not have access to this property.")


def ensure_record_owner(user: User, owner_user_id: UUID) -> None:
    """Personal/assigned records require their own ownership check, even inside a property."""
    if user.role_id != "SUPER_ADMIN" and user.id != owner_user_id:
        raise HTTPException(403, "You do not have access to this record.")


def require_trusted_origin(request: Request,
    origin: str | None = Header(None),
    csrf: str | None = Header(None, alias="X-StayOps-CSRF")) -> None:
    # Custom header forces browser preflight; exact Origin check also blocks same-site sibling origins.
    if origin not in get_settings().cors_origins or csrf != "1":
        raise HTTPException(403, "Untrusted request origin.")


def trusted_property_ids(user):
    if user.role_id == 'OWNER':
        from sqlalchemy import select
        from sqlalchemy.orm import object_session
        from app.modules.properties.models import Property
        db = object_session(user)
        return set(db.scalars(select(Property.id).where(Property.owner_user_id == user.id))) if db else set()
    return {item.property_id for item in user.property_assignments}
