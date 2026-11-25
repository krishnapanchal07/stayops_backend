import hashlib
import io
import warnings
from pathlib import PurePosixPath
from uuid import uuid4
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import HTTPException
from app.modules.properties.models import PropertyImage
from app.modules.properties.storage import storage

MAX_IMAGE_SIZE_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 24_000_000
VARIANTS = {'thumbnail': (400, 75), 'medium': (900, 80), 'large': (1600, 82)}
FORMATS = {'JPEG': {'.jpg','.jpeg'}, 'PNG': {'.png'}, 'WEBP': {'.webp'}}


async def read_upload(upload):
    content = bytearray()
    while chunk := await upload.read(min(65536, MAX_IMAGE_SIZE_BYTES - len(content))):
        content.extend(chunk)
        if len(content) >= MAX_IMAGE_SIZE_BYTES:
            raise HTTPException(413, 'Every image must be less than 4 MB.')
    return bytes(content)


def process(content, filename, content_type):
    if not content:
        raise HTTPException(400, 'Empty image file.')
    if len(content) >= MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(413, 'Every image must be less than 4 MB.')
    filename = PurePosixPath((filename or '').replace('\\','/')).name[:160]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                fmt = source.format
                if fmt not in FORMATS or PurePosixPath(filename).suffix.lower() not in FORMATS[fmt] or content_type != {'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}[fmt]:
                    raise HTTPException(400, 'Unsupported or mismatched image format. Upload JPG, PNG, or WebP.')
                if source.width * source.height > MAX_PIXELS or max(source.size) > 12000 or getattr(source, 'n_frames', 1) != 1:
                    raise HTTPException(422, 'Image exceeds dimension limits or contains animation.')
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source).convert('RGBA' if 'A' in source.getbands() or 'transparency' in source.info else 'RGB')
                normalized.info.clear()
            variants = {}
            for name, (width, quality) in VARIANTS.items():
                copy = normalized.copy()
                if copy.width > width:
                    copy = copy.resize((width, max(1, round(copy.height * width / copy.width))), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                copy.save(buffer, format='WEBP', quality=quality, method=4)
                encoded = buffer.getvalue()
                # Flat PNGs can legitimately be smaller than lossy WebP. Use lossless when beneficial.
                if len(encoded) > len(content):
                    buffer = io.BytesIO()
                    copy.save(buffer, format='WEBP', lossless=True, method=4)
                    if buffer.tell() < len(encoded):
                        encoded = buffer.getvalue()
                if len(encoded) >= MAX_IMAGE_SIZE_BYTES:
                    raise HTTPException(422, 'Optimized image is too complex; choose a smaller image.')
                variants[name] = (encoded, copy.width, copy.height)
            return filename, normalized.size, hashlib.sha256(content).hexdigest(), variants
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(400, 'Invalid, corrupted or excessively large image content.') from None


def persist_batch(db, property, prepared, image_model=PropertyImage):
    from app.core.config import get_settings
    known = {image.content_digest for image in property.images}
    unique = []
    for item in prepared:
        if item[2] not in known:
            unique.append(item)
            known.add(item[2])
    if len(property.images) + len(unique) > get_settings().property_max_images:
        raise HTTPException(422, f'A property may have at most {get_settings().property_max_images} images.')
    written = []
    try:
        for filename, dimensions, digest, variants, original_size in unique:
            key = uuid4().hex
            metadata = {}
            for variant, (content, width, height) in variants.items():
                path = f'{key}/{variant}.webp'
                storage.put(path, content)
                written.append(path)
                metadata[variant] = {'key':path, 'width':width, 'height':height, 'bytes':len(content)}
            property.images.append(image_model(storage_key=key, original_filename_display=filename,
                image_width=dimensions[0],image_height=dimensions[1],original_file_size=original_size,
                content_digest=digest,display_order=len(property.images),is_cover=not property.images,variants=metadata))
        db.commit()
    except Exception:
        db.rollback()
        for key in written:
            storage.delete(key)
        raise
