"""Real PostgreSQL tests in a disposable local cluster. Never use stayops_db."""
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET_KEY"] = "isolated-test-key-" + "a" * 48
os.environ["DATABASE_URL"] = "postgresql+psycopg://stayops_test@127.0.0.1:55432/postgres"

from app.core.config import get_settings
get_settings.cache_clear()

from app.core.rate_limit import limiter
from app.core.security import password_hasher
from app.db.session import get_db
from app.main import create_app
from app.modules.auth import service
from app.modules.auth.models import User
from app.modules.auth.seed import seed_roles
from fastapi.testclient import TestClient

PASSWORD = "Test-password-12345"
HEADERS = {"Origin": "http://localhost:5173", "X-StayOps-CSRF": "1"}


def run_pg(arguments):
    # A Windows postmaster can inherit captured pipe handles, preventing communicate()
    # from ever seeing EOF. A disk-backed temporary file avoids that startup deadlock.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run([str(a) for a in arguments], stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            output.seek(0)
            pytest.fail(output.read().decode(errors="replace"))
        return result


@pytest.fixture(scope="session")
def postgres_url():
    binary = Path(os.environ.get("POSTGRES_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
    executable = ".exe" if os.name == "nt" else ""
    initdb, pg_ctl = binary / ("initdb" + executable), binary / ("pg_ctl" + executable)
    if not initdb.exists():
        pytest.fail("PostgreSQL binaries not found. Set POSTGRES_BIN to your PostgreSQL bin directory.")
    folder = Path(tempfile.mkdtemp(prefix="stayops-auth-test-"))
    cluster = folder / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    started = False
    try:
        run_pg([initdb, "-D", cluster, "-A", "trust", "-U", "stayops_test", "--encoding=UTF8", "--no-locale"])
        # Disposable trust-auth cluster binds only loopback on a random port.
        run_pg([pg_ctl, "-D", cluster, "-l", folder / "postgres.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"])
        started = True
        yield f"postgresql+psycopg://stayops_test@127.0.0.1:{port}/postgres"
    finally:
        if started:
            run_pg([pg_ctl, "-D", cluster, "-m", "fast", "-w", "stop"])
        # Delete only the exact directory returned by mkdtemp, under the OS temp root.
        if folder.parent.resolve() == Path(tempfile.gettempdir()).resolve() and folder.name.startswith("stayops-auth-test-"):
            shutil.rmtree(folder)


@pytest.fixture
def setup(postgres_url, monkeypatch):
    schema = "auth_test_" + uuid4().hex
    admin = create_engine(postgres_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(postgres_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        seed_roles(db)
        # Isolated legacy inventory fixtures are explicitly published for public-inquiry tests.
        from app.modules.properties.models import Property
        from sqlalchemy import update
        db.execute(update(Property).values(is_public=True))
        hashed = password_hasher.hash(PASSWORD)
        for role, email in [("SUPER_ADMIN", "admin@example.com"), ("TENANT", "tenant@example.com"), ("OWNER", "owner@example.com")]:
            db.add(User(full_name=role.replace("_", " ").title(), email=email, role_id=role, password_hash=hashed))
        db.add(User(full_name="Inactive", email="inactive@example.com", role_id="TENANT", password_hash=hashed, is_active=False))
        db.commit()
    limiter.entries.clear()
    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    from app.modules.auth.dependencies import require_permission, require_role
    from fastapi import Depends

    @app.get("/test/admin")
    def admin_only(user=Depends(require_role("SUPER_ADMIN"))):
        return {"ok": True}

    @app.get("/test/permission")
    def permission_only(user=Depends(require_permission("PROPERTY_DELETE"))):
        return {"ok": True}

    sent = []
    monkeypatch.setattr(service.email_service, "send_reset_email", lambda email, link: sent.append((email, link)))
    with TestClient(app, headers=HEADERS) as client:
        yield client, factory, sent, engine, config
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.fixture
def client(setup):
    return setup[0]


def sign_in(client, email="tenant@example.com", **extra):
    return client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD, **extra})


def authorization(response):
    return {"Authorization": "Bearer " + response.json()["access_token"]}
