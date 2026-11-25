from app.modules.inventory import models as inventory_models
"""Run from Backend: python -m app.cli seed | bootstrap | cleanup"""
import argparse
from getpass import getpass

from sqlalchemy import delete, select

from app.core.security import now, password_hasher
from app.db.session import get_session_factory
from app.modules.auth.models import AuthSession, PasswordResetToken, User
from app.modules.auth.schemas import ProvisionInput
from app.modules.auth.seed import seed_roles
from app.modules.properties import models as property_models  # register assignment FK


def main() -> None:
    parser = argparse.ArgumentParser(description="StayOps authentication administration")
    parser.add_argument("command", choices=["seed", "bootstrap", "cleanup"])
    command = parser.parse_args().command
    with get_session_factory()() as db:
        if command == "cleanup":
            # Keep consumed refresh history until its entire session expires.
            db.execute(delete(AuthSession).where(AuthSession.expires_at < now()))
            db.execute(delete(PasswordResetToken).where(PasswordResetToken.expires_at < now()))
        else:
            seed_roles(db)
            if command == "bootstrap":
                # PostgreSQL advisory lock prevents concurrent first-admin bootstraps.
                from sqlalchemy import text
                db.execute(text("SELECT pg_advisory_xact_lock(782463910)"))
                if db.scalar(select(User.id).where(User.role_id == "SUPER_ADMIN").limit(1)):
                    raise SystemExit("A super-admin already exists. Use authenticated provisioning.")
                name, email = input("Full name: "), input("Email: ")
                password = getpass("Password (12-128 characters, letter and number): ")
                if password != getpass("Confirm password: "):
                    raise SystemExit("Passwords do not match.")
                record = ProvisionInput(full_name=name, email=email, password=password, role="SUPER_ADMIN")
                db.add(User(full_name=record.full_name, email=str(record.email), role_id=record.role,
                            password_hash=password_hasher.hash(record.password)))
        db.commit()
    print("Completed.")


if __name__ == "__main__":
    main()
