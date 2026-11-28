from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import now
from app.db.base import Base
from app.modules.finance.models import Audit


class Complaint(Audit, Base):
    __tablename__ = 'complaints'
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','REVIEWED','ASSIGNED','IN_PROGRESS','RESOLVED','CLOSED','REJECTED','ESCALATED')", name='status'),
        CheckConstraint("priority IN ('LOW','MEDIUM','HIGH','URGENT')", name='priority'),
        CheckConstraint("category IN ('PLUMBING','ELECTRICAL','INTERNET','CLEANING','FURNITURE','AC','WATER','SECURITY','ROOM','NOISE','HOUSEKEEPING','OTHER')", name='category'),
        CheckConstraint("source IN ('TENANT','ADMIN','WARDEN')", name='source'),
        CheckConstraint('actual_cost IS NULL OR actual_cost >= 0', name='actual_cost'),
        CheckConstraint('estimated_cost IS NULL OR estimated_cost >= 0', name='estimated_cost'),
        CheckConstraint('escalation_level >= 0', name='escalation'),
        Index('ix_complaints_property_status', 'property_id', 'status'),
    )
    code: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: 'CMP-' + uuid4().hex.upper())
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'))
    room_id: Mapped[str] = mapped_column(ForeignKey('rooms.id'), index=True)
    tenant_name: Mapped[str] = mapped_column(String(260))
    property_name: Mapped[str] = mapped_column(String(160))
    room_number: Mapped[str] = mapped_column(String(40))
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey('staff_profiles.user_id'), index=True)
    assigned_name: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(20), default='OPEN')
    source: Mapped[str] = mapped_column(String(12))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_notes: Mapped[str] = mapped_column(Text, default='')
    escalation_level: Mapped[int] = mapped_column(default=0)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class ComplaintRecord:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    complaint_id: Mapped[UUID] = mapped_column(ForeignKey('complaints.id'), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Comment(ComplaintRecord, Base):
    __tablename__ = 'complaint_comments'
    __table_args__ = (CheckConstraint("visibility IN ('PUBLIC','INTERNAL')", name='visibility'),)
    actor_name: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(12))


class Event(ComplaintRecord, Base):
    __tablename__ = 'complaint_events'
    event_type: Mapped[str] = mapped_column(String(32))
    actor_name: Mapped[str] = mapped_column(String(120))
    actor_role: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(Text)
    previous_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    internal: Mapped[bool] = mapped_column(default=False)


class Attachment(ComplaintRecord, Base):
    __tablename__ = 'complaint_attachments'
    __table_args__ = (CheckConstraint('file_size > 0 AND file_size < 4194304', name='file_size'),)
    file_name: Mapped[str] = mapped_column(String(160))
    file_size: Mapped[int]


class Task(Base):
    __tablename__ = 'maintenance_tasks'
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','ASSIGNED','IN_PROGRESS','COMPLETED','CANCELLED')", name='status'),
        CheckConstraint('actual_cost IS NULL OR actual_cost >= 0', name='cost'),
        Index('uq_maintenance_tasks_active_complaint', 'complaint_id', unique=True,
              postgresql_where=text("status IN ('PENDING','ASSIGNED','IN_PROGRESS')")),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: 'MT-' + uuid4().hex.upper())
    complaint_id: Mapped[UUID] = mapped_column(ForeignKey('complaints.id'), index=True)
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey('staff_profiles.user_id'), index=True)
    assigned_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default='ASSIGNED', index=True)
    scheduled_date: Mapped[date | None]
    priority: Mapped[str] = mapped_column(String(12))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    parts_used: Mapped[str] = mapped_column(Text, default='')
    resolution_notes: Mapped[str] = mapped_column(Text, default='')
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
