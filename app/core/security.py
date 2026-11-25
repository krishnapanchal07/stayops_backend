import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

password_hasher = PasswordHash.recommended()
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def now() -> datetime:
    return datetime.now(UTC)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(48)


def access_token(user_id: UUID, session_id: UUID) -> str:
    settings = get_settings()
    issued = now()
    return jwt.encode({"sub": str(user_id), "sid": str(session_id), "iat": issued,
                       "exp": issued + timedelta(minutes=settings.access_token_expire_minutes),
                       "iss": "stayops", "aud": "stayops-api", "type": "access", "jti": str(uuid4())},
                      settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm)


def decode_access(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret_key.get_secret_value(),
                      algorithms=[settings.jwt_algorithm], issuer="stayops", audience="stayops-api",
                      options={"require": ["sub", "sid", "iat", "exp", "type"]})
