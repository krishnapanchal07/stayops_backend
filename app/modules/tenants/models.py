from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import now
from app.db.base import Base


class Tenant(Base):
    __tablename__ = 'tenants'
    __table_args__ = (
        ForeignKeyConstraint(['bed_id','room_id'], ['beds.id','beds.room_id'], name='fk_tenants_bed_room_beds'),
        CheckConstraint("status IN ('PROSPECT','ONBOARDING','ACTIVE','NOTICE_PERIOD','CHECKED_OUT','INACTIVE')", name='valid_status'),
        CheckConstraint("source IN ('DIRECT','WALK_IN','INQUIRY')", name='valid_source'),
        CheckConstraint('email = lower(trim(email))', name='email_normalized'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_code: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: 'TEN-' + uuid4().hex.upper())
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    property_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("properties.id"), index=True)
    room_id: Mapped[str | None] = mapped_column(String(64), ForeignKey('rooms.id'), index=True)
    bed_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default='ONBOARDING', index=True)
    source: Mapped[str] = mapped_column(String(16), default='DIRECT')
    inquiry_id: Mapped[UUID | None] = mapped_column(ForeignKey('inquiries.id', use_alter=True, name='fk_tenants_inquiry_id_inquiries'), unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    internal_notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Occupancy(Base):
    """Active allocations only; release deletes a ledger entry, activity preserves history."""
    __tablename__ = 'tenant_occupancies'
    __table_args__ = (ForeignKeyConstraint(['bed_id','room_id'], ['beds.id','beds.room_id'], name='fk_tenant_occupancies_bed_room_beds'),)
    bed_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), unique=True)
    room_id: Mapped[str] = mapped_column(String(64), ForeignKey('rooms.id'), index=True)
    property_id: Mapped[str] = mapped_column(String(64), ForeignKey("properties.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantActivity(Base):
    __tablename__ = 'tenant_activity'
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    actor_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(2000))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantInvitation(Base):
    __tablename__ = 'tenant_invitations'
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
