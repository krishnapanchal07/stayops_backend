"""Remove old orphaned local image variants after interrupted writes.

Explicit operator command: python -m app.modules.properties.maintenance
Only UUID-named image objects older than 24 hours, with no DB reference, qualify.
Reusable demo source photographs are outside this directory and never touched.
"""
import re
import time
from pathlib import Path
from sqlalchemy import select
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.modules.auth import models as auth_models
from app.modules.properties.models import PropertyImage
from app.modules.inventory.models import RoomImage
from app.modules.properties.storage import storage


def cleanup_orphans(db, minimum_age=86400):
    root = Path(get_settings().property_image_directory).resolve()
    if not root.exists():
        return 0
    referenced = set(db.scalars(select(PropertyImage.storage_key))) | set(db.scalars(select(RoomImage.storage_key)))
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or not re.fullmatch('[a-f0-9]{32}',folder.name) or folder.name in referenced:
            continue
        files = list(folder.iterdir())
        if not files or any(f.name not in {'thumbnail.webp','medium.webp','large.webp'} or not f.is_file() or f.is_symlink() or time.time()-f.stat().st_mtime < minimum_age for f in files):
            continue
        # Recheck after the age gate. In-progress requests cannot last a day.
        if db.scalar(select(PropertyImage.id).where(PropertyImage.storage_key==folder.name)) or db.scalar(select(RoomImage.id).where(RoomImage.storage_key==folder.name)):
            continue
        for file in files:
            storage.delete(folder.name+'/'+file.name)
            removed += 1
    return removed


if __name__ == '__main__':
    with get_session_factory()() as db:
        print('Old orphan variants removed:', cleanup_orphans(db))
