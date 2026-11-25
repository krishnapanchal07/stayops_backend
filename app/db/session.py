from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine():
    return create_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True,
                         pool_size=5, max_overflow=10, hide_parameters=True)


@lru_cache
def get_session_factory():
    return sessionmaker(get_engine(), expire_on_commit=False)


def get_db():
    with get_session_factory()() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise
