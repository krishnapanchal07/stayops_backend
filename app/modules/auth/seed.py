import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.modules.auth.models import Permission, Role


def seed_roles(db: Session) -> None:
    """Versioned snapshot of the existing UI contract; never imports mock users."""
    folder = Path(__file__).parent
    for record in json.loads((folder / "permissions.json").read_text(encoding="utf-8")):
        permission = db.get(Permission, record["id"])
        if permission is None:
            permission = Permission(id=record["id"], name=record["name"])
            db.add(permission)
        permission.name = record["name"]
    db.flush()
    for record in json.loads((folder / "roles.json").read_text(encoding="utf-8")):
        role = db.get(Role, record["id"])
        if role is None:
            role = Role(id=record["id"], name=record["name"])
            db.add(role)
        role.name = record["name"]
        role.permissions = [db.get(Permission, key) for key in record["permissions"]]
    db.flush()
