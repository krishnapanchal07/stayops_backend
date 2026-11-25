from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.core.security import now


class Property(Base):
    __tablename__ = 'properties'
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','INACTIVE','MAINTENANCE','ARCHIVED')", name='valid_status'),
        CheckConstraint("type IN ('PG','HOSTEL','CO_LIVING')", name='valid_type'),
        CheckConstraint('(latitude IS NULL AND longitude IS NULL) OR (latitude BETWEEN -85.05112878 AND 85.05112878 AND longitude BETWEEN -180 AND 180)', name='coordinates'),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(80), index=True)
    type: Mapped[str] = mapped_column(String(16), index=True)
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'), index=True)
    address: Mapped[str] = mapped_column(String(160))
    locality: Mapped[str] = mapped_column(String(100), default='')
    city: Mapped[str] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(String(100))
    postal_code: Mapped[str] = mapped_column(String(16), default='')
    country: Mapped[str] = mapped_column(String(80), default='India')
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str] = mapped_column(String(500), default='')
    contact_number: Mapped[str] = mapped_column(String(20), default='')
    email: Mapped[str] = mapped_column(String(320), default='')
    amenities: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default='ACTIVE', index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    images: Mapped[list['PropertyImage']] = relationship(lazy='selectin', order_by='PropertyImage.display_order', cascade='all, delete-orphan')


class PropertyImage(Base):
    __tablename__ = 'property_images'
    __table_args__ = (UniqueConstraint('property_id', 'content_digest'),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'), index=True)
    storage_key: Mapped[str] = mapped_column(String(80), unique=True)
    original_filename_display: Mapped[str] = mapped_column(String(160))
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    original_file_size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(32), default='image/webp')
    content_digest: Mapped[str] = mapped_column(String(64))
    display_order: Mapped[int] = mapped_column(Integer)
    is_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    variants: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
