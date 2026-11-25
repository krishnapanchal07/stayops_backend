from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import now
from app.modules.auth.models import AuthSession, User


def user_by_email(db: Session, email: str, *, lock: bool = False) -> User | None:
    query = select(User).where(User.email == email.strip().lower())
    if lock:
        query = query.with_for_update()
    return db.scalar(query.execution_options(populate_existing=True))


def lock_user(db: Session, user_id: UUID) -> User | None:
    return db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))


def revoke_sessions(db: Session, user_id: UUID) -> None:
    db.execute(update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)).values(revoked_at=now()))
