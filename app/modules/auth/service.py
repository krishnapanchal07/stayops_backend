import logging
from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import DUMMY_HASH, access_token, digest, now, password_hasher, random_token
from app.db.session import get_session_factory
from app.modules.auth import repository
from app.modules.auth.dependencies import unauthorized
from app.modules.auth.models import AuthSession, PasswordResetToken, RefreshToken, User
from app.modules.auth.schemas import PublicUser, SessionOutput, TokenOutput
from app.services import email_service

logger = logging.getLogger("stayops.auth")
RESET_MESSAGE = "If an account exists with this email, a password reset link has been sent."


def public_session(user: User) -> SessionOutput:
    return SessionOutput(user=PublicUser(id=str(user.id), name=user.full_name, email=user.email,
        roleId=user.role_id, role=user.role_id, roleLabel=user.role.name,
        initials="".join(word[0].upper() for word in user.full_name.split()[:2]),
        status="ACTIVE" if user.is_active else "INACTIVE", avatar=user.avatar,
        propertyIds=[item.property_id for item in user.property_assignments]),
        role=user.role_id, permissions=sorted(permission.id for permission in user.role.permissions))


def tokens(user: User, session: AuthSession) -> TokenOutput:
    return TokenOutput(**public_session(user).model_dump(), access_token=access_token(user.id, session.id),
                       expires_in=get_settings().access_token_expire_minutes * 60)


def login(db: Session, email: str, password: str, remember: bool):
    # User lock serializes password changes and new sessions.
    user = repository.user_by_email(db, email, lock=True)
    valid, updated_hash = password_hasher.verify_and_update(password, user.password_hash if user else DUMMY_HASH)
    if not user or not valid or not user.is_active:
        raise HTTPException(401, "Incorrect email or password.")
    if updated_hash:
        user.password_hash = updated_hash
    session = AuthSession(user_id=user.id, expires_at=now() + timedelta(days=get_settings().refresh_token_expire_days), persistent=remember)
    db.add(session)
    db.flush()
    raw = random_token()
    db.add(RefreshToken(session_id=session.id, token_digest=digest(raw)))
    user.last_login_at = now()
    db.commit()
    return tokens(user, session), raw, session


def resolve_refresh(db: Session, raw: str):
    token = db.scalar(select(RefreshToken).where(RefreshToken.token_digest == digest(raw)))
    if not token:
        raise unauthorized()
    initial_session = db.get(AuthSession, token.session_id)
    if not initial_session:
        raise unauthorized()
    # Uniform lock order throughout: user, then session. Re-read token after the locks.
    user = repository.lock_user(db, initial_session.user_id)
    session = db.scalar(select(AuthSession).where(AuthSession.id == token.session_id).with_for_update().execution_options(populate_existing=True))
    db.refresh(token)
    if not user or not user.is_active or not session or session.revoked_at or session.expires_at <= now():
        raise unauthorized()
    return user, session, token


def refresh(db: Session, raw: str):
    user, session, previous = resolve_refresh(db, raw)
    if previous.used_at:
        # Keep consumed digests until session cleanup so replay revokes the entire family.
        session.revoked_at = now()
        db.commit()
        raise unauthorized()
    previous.used_at = now()
    replacement = random_token()
    db.add(RefreshToken(session_id=session.id, token_digest=digest(replacement)))
    db.commit()
    return tokens(user, session), replacement, session


def logout(db: Session, raw: str) -> None:
    try:
        _, session, _ = resolve_refresh(db, raw)
    except HTTPException:
        db.rollback()
        return
    session.revoked_at = now()
    db.commit()


def deliver_reset(email: str) -> None:
    """Runs after the uniform HTTP response. Failures never expose credentials or tokens."""
    try:
        with get_session_factory()() as db:
            user = repository.user_by_email(db, email, lock=True)
            if not user or not user.is_active:
                return
            issued = now()
            db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None)).values(used_at=issued))
            raw = random_token()
            db.add(PasswordResetToken(user_id=user.id, token_digest=digest(raw),
                expires_at=issued + timedelta(minutes=get_settings().reset_token_expire_minutes)))
            db.flush()
            # Fragment keeps the token out of HTTP URLs, server access logs and referrers.
            link = get_settings().frontend_url.rstrip("/") + "/reset-password#token=" + quote(raw, safe="")
            email_service.send_reset_email(user.email, link)
            db.commit()
    except Exception:
        # Context close rolls back failed delivery, including invalidation of previous links.
        logger.warning("Password reset delivery failed")


def reset_password(db: Session, raw: str, password: str) -> None:
    token = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_digest == digest(raw)))
    if not token:
        raise HTTPException(400, "Reset link is invalid or expired.")
    user = repository.lock_user(db, token.user_id)
    db.refresh(token)
    if not user or not user.is_active or token.used_at or token.expires_at <= now():
        raise HTTPException(400, "Reset link is invalid or expired.")
    user.password_hash = password_hasher.hash(password)
    invalidate_credentials(db, user)
    db.commit()


def invalidate_credentials(db: Session, user: User) -> None:
    repository.revoke_sessions(db, user.id)
    db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None)).values(used_at=now()))
