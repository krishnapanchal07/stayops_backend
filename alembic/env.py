from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db.base import Base
from app.modules.auth import models  # noqa: F401: register all auth models
from app.modules.inquiries import models as inquiry_models  # noqa: F401
from app.modules.tenants import models as tenant_models  # noqa: F401
from app.modules.staff import models as staff_models  # noqa: F401

from app.modules.properties import models as property_models
from app.modules.inventory import models as inventory_models

config = context.config
target_metadata = Base.metadata


def run_migrations():
    # Tests pass an already opened connection. No secret is written to alembic.ini.
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = get_settings().database_url.get_secret_value()
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


run_migrations()
