from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.security import now, password_hasher
from app.db.session import get_db
from app.modules.auth import repository, service
from app.modules.auth.dependencies import get_active_user, require_role, require_trusted_origin
from app.modules.auth.models import AuthSession, Role, User
from app.modules.auth.schemas import (ChangePasswordInput, EmailInput, LoginInput, MessageOutput,
    ProfileInput, ProvisionInput, ResetInput, SessionOutput, TokenOutput)

router = APIRouter(prefix="/auth", tags=["Authentication"])
COOKIE_NAME = "stayops_refresh"
COOKIE_PATH = "/api/v1/auth"


def set_refresh_cookie(response: Response, raw: str, session: AuthSession) -> None:
    settings = get_settings()
    response.set_cookie(COOKIE_NAME, raw, max_age=max(0, int((session.expires_at - now()).total_seconds())) if session.persistent else None,
        httponly=True, secure=settings.cookie_secure, samesite=settings.cookie_samesite, path=COOKIE_PATH)


def clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True,
                           secure=settings.cookie_secure, samesite=settings.cookie_samesite)


@router.post("/login", response_model=TokenOutput, dependencies=[Depends(require_trusted_origin)])
def login(payload: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.auth(request, "login", str(payload.email))
    result, raw, session = service.login(db, str(payload.email), payload.password, payload.remember)
    set_refresh_cookie(response, raw, session)
    return result


@router.post("/refresh", response_model=TokenOutput, dependencies=[Depends(require_trusted_origin)])
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.check("refresh:" + (request.client.host if request.client else "unknown"), 120)
    result, raw, session = service.refresh(db, request.cookies.get(COOKIE_NAME, ""))
    set_refresh_cookie(response, raw, session)
    return result


@router.post("/logout", response_model=MessageOutput, dependencies=[Depends(require_trusted_origin)])
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    service.logout(db, request.cookies.get(COOKIE_NAME, ""))
    clear_refresh_cookie(response)
    return {"message": "Signed out."}


@router.get("/me", response_model=SessionOutput)
def me(user: User = Depends(get_active_user)):
    return service.public_session(user)


@router.post("/forgot-password", response_model=MessageOutput, status_code=202, dependencies=[Depends(require_trusted_origin)])
def forgot_password(payload: EmailInput, request: Request, background: BackgroundTasks):
    limiter.auth(request, "forgot", str(payload.email))
    background.add_task(service.deliver_reset, str(payload.email))
    return {"message": service.RESET_MESSAGE}


@router.post("/reset-password", response_model=MessageOutput, dependencies=[Depends(require_trusted_origin)])
def reset_password(payload: ResetInput, request: Request, response: Response, db: Session = Depends(get_db)):
    limiter.auth(request, "reset")
    service.reset_password(db, payload.token, payload.new_password)
    clear_refresh_cookie(response)
    return {"message": "Password updated. Sign in with your new password."}


@router.post("/users", response_model=SessionOutput, status_code=201, dependencies=[Depends(require_trusted_origin)])
def provision_user(payload: ProvisionInput, admin: User = Depends(require_role("SUPER_ADMIN")), db: Session = Depends(get_db)):
    if not db.get(Role, payload.role):
        raise HTTPException(400, "Initialize roles before provisioning users.")
    user = User(full_name=payload.full_name, email=str(payload.email), role_id=payload.role,
                password_hash=password_hasher.hash(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return service.public_session(user)


@router.post("/change-password", response_model=MessageOutput, dependencies=[Depends(require_trusted_origin)])
def change_password(payload: ChangePasswordInput, response: Response, current: User = Depends(get_active_user), db: Session = Depends(get_db)):
    user = repository.lock_user(db, current.id)
    if not password_hasher.verify(payload.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    user.password_hash = password_hasher.hash(payload.new_password)
    service.invalidate_credentials(db, user)
    db.commit()
    clear_refresh_cookie(response)
    return {"message": "Password updated. Sign in again."}


@router.patch("/me", response_model=SessionOutput, dependencies=[Depends(require_trusted_origin)])
def update_profile(payload: ProfileInput, current: User = Depends(get_active_user), db: Session = Depends(get_db)):
    user = repository.lock_user(db, current.id)
    user.full_name, user.avatar = payload.name, payload.avatar
    db.commit()
    return service.public_session(user)
