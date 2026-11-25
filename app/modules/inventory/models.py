"""Canonical inventory. Occupancy is owned exclusively by tenant_occupancies."""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.core.security import now


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Building(Timestamps, Base):
    __tablename__ = 'buildings'
    __table_args__ = (UniqueConstraint('property_id','name'), UniqueConstraint('property_id','building_code',name='uq_buildings_property_code'),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','ARCHIVED')", name='valid_status'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    building_code: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000), default='')
    status: Mapped[str] = mapped_column(String(16), default='ACTIVE')
    property: Mapped['Property'] = relationship()
    floors: Mapped[list['Floor']] = relationship(lazy='selectin', back_populates='building')


class Floor(Timestamps, Base):
    __tablename__ = 'floors'
    __table_args__ = (UniqueConstraint('building_id','floor_number'),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','ARCHIVED')", name='valid_status'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    building_id: Mapped[str] = mapped_column(ForeignKey('buildings.id'), index=True)
    floor_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000), default='')
    status: Mapped[str] = mapped_column(String(16), default='ACTIVE')
    building: Mapped[Building] = relationship(back_populates='floors')


class Room(Timestamps, Base):
    __tablename__ = 'rooms'
    __table_args__ = (UniqueConstraint('floor_id','room_number'),
        CheckConstraint('capacity BETWEEN 1 AND 4', name='valid_capacity'),
        CheckConstraint('monthly_rent > 0 AND security_deposit >= 0', name='valid_money'),
        CheckConstraint("room_type IN ('STANDARD','PREMIUM','DELUXE')", name='valid_type'),
        CheckConstraint("sharing_type IN ('1_SHARING','2_SHARING','3_SHARING','4_SHARING')", name='valid_sharing'),
        CheckConstraint("capacity = CAST(substring(sharing_type,1,1) AS INTEGER)", name='sharing_capacity'),
        CheckConstraint("status IN ('AVAILABLE','MAINTENANCE','BLOCKED','ARCHIVED')", name='valid_status'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    floor_id: Mapped[str] = mapped_column(ForeignKey('floors.id'), index=True)
    room_number: Mapped[str] = mapped_column(String(40))
    room_type: Mapped[str] = mapped_column(String(16))
    sharing_type: Mapped[str] = mapped_column(String(16))
    capacity: Mapped[int] = mapped_column(Integer)
    monthly_rent: Mapped[Decimal] = mapped_column(Numeric(12,2))
    security_deposit: Mapped[Decimal] = mapped_column(Numeric(12,2))
    status: Mapped[str] = mapped_column(String(16), default='AVAILABLE', index=True)
    amenities: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(2000), default='')
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    floor: Mapped[Floor] = relationship()
    beds: Mapped[list['Bed']] = relationship(lazy='selectin', back_populates='room', order_by='Bed.bed_number')
    images: Mapped[list['RoomImage']] = relationship(lazy='selectin', order_by='RoomImage.display_order', cascade='all, delete-orphan')


class Bed(Timestamps, Base):
    __tablename__ = 'beds'
    __table_args__ = (UniqueConstraint('room_id','bed_number'), UniqueConstraint('id','room_id'),
        CheckConstraint('rent > 0', name='valid_rent'),
        CheckConstraint("status IN ('AVAILABLE','RESERVED','MAINTENANCE','BLOCKED')", name='valid_status'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    room_id: Mapped[str] = mapped_column(ForeignKey('rooms.id'), index=True)
    bed_number: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default='AVAILABLE')
    rent: Mapped[Decimal] = mapped_column(Numeric(12,2))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    room: Mapped[Room] = relationship(back_populates='beds')


from app.modules.properties.models import Property, PropertyImage
# Share the media metadata contract and processor, with a separate relational owner.
class RoomImage(Base):
    __tablename__ = 'room_images'
    __table_args__ = (UniqueConstraint('room_id','content_digest'),)
    id = mapped_column(PropertyImage.__table__.c.id.type, primary_key=True, default=uuid4)
    room_id: Mapped[str] = mapped_column(ForeignKey('rooms.id'), index=True)
    storage_key: Mapped[str] = mapped_column(String(80), unique=True)
    original_filename_display: Mapped[str] = mapped_column(String(160))
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    original_file_size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(32), default='image/webp')
    content_digest: Mapped[str] = mapped_column(String(64))
    display_order: Mapped[int] = mapped_column(Integer)
    is_cover: Mapped[bool] = mapped_column(default=False)
    variants: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
