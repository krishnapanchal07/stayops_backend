"""Immutable local object store. Business logic depends on this small interface only."""
import re
from pathlib import Path
from typing import Protocol
from app.core.config import get_settings


class ImageStorage(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def delete(self, key: str) -> None: ...
    def path(self, key: str) -> Path: ...


class LocalImageStorage:
    def path(self, key):
        if not re.fullmatch(r'[a-f0-9]{32}/(thumbnail|medium|large)\.webp', key):
            raise ValueError('Invalid storage key')
        root = Path(get_settings().property_image_directory).resolve()
        result = (root / key).resolve()
        if not result.is_relative_to(root):
            raise ValueError('Invalid storage path')
        return result

    def put(self, key, content):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: random object keys are never overwritten.
        try:
            with target.open('xb') as file:
                file.write(content)
        except Exception:
            self.delete(key)
            raise

    def delete(self, key):
        target = self.path(key)
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
        except OSError:
            pass


storage: ImageStorage = LocalImageStorage()
