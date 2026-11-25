from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, JSON, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import now
from app.db.base import Base

OPEN = "status IN ('SUBMITTED','CONTACTED','SHORTLISTED','APPROVED')"


class Inquiry(Base):
    __tablename__ = 'inquiries'
    __table_args__ = (
        ForeignKeyConstraint(['bed_id','room_id'], ['beds.id','beds.room_id'], name='fk_inquiries_bed_room_beds'),
        CheckConstraint("status IN ('SUBMITTED','CONTACTED','SHORTLISTED','APPROVED','CONVERTED','REJECTED','WITHDRAWN')", name='valid_status'),
        CheckConstraint('email = lower(trim(email))', name='email_normalized'),
        CheckConstraint("(kind = 'ROOM' AND property_id IS NOT NULL AND room_id IS NOT NULL) OR (kind IN ('tenant','owner','staff') AND property_id IS NULL AND room_id IS NULL AND bed_id IS NULL)", name='kind_reference'),
        Index('uq_inquiries_open_target', 'email', 'room_id', func.coalesce(text('bed_id'), ''), unique=True, postgresql_where=text(OPEN)),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), default='ROOM')
    email: Mapped[str] = mapped_column(String(320), index=True)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'), index=True)
    property_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("properties.id"), index=True)
    room_id: Mapped[str | None] = mapped_column(String(64), ForeignKey('rooms.id'), index=True)
    bed_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default='SUBMITTED', index=True)
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey('tenants.id', use_alter=True, name='fk_inquiries_tenant_id_tenants'), unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    events: Mapped[list['InquiryStatusEvent']] = relationship(lazy='selectin', order_by='InquiryStatusEvent.occurred_at')


class InquiryStatusEvent(Base):
    __tablename__ = 'inquiry_status_events'
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    inquiry_id: Mapped[UUID] = mapped_column(ForeignKey('inquiries.id'), index=True)
    previous: Mapped[str | None] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    actor_name: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
