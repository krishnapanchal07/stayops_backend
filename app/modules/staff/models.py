from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, JSON, Sequence, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import now
from app.db.base import Base
from app.modules.auth.models import User

employee_codes = Sequence('staff_employee_code_seq', start=1001, metadata=Base.metadata)


class StaffProfile(Base):
    __tablename__ = 'staff_profiles'
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','INACTIVE','ON_LEAVE')", name='valid_status'),
        CheckConstraint("department IN ('OPERATIONS','FINANCE','SECURITY','MAINTENANCE')", name='valid_department'),
        CheckConstraint("phone ~ '^[0-9]{10}$'", name='valid_phone'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), unique=True)
    employee_code: Mapped[str] = mapped_column(String(40), unique=True,
        server_default=text("'STF-' || nextval('staff_employee_code_seq')::text"))
    phone: Mapped[str] = mapped_column(String(10))
    department: Mapped[str] = mapped_column(String(24), index=True)
    designation: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), index=True)
    joining_date: Mapped[date] = mapped_column(Date)
    address: Mapped[dict] = mapped_column(JSON)
    emergency_contact: Mapped[dict] = mapped_column(JSON)
    link_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    user: Mapped[User] = relationship(lazy='selectin')
    invitation: Mapped['StaffLinkInvitation | None'] = relationship(lazy='selectin', uselist=False)


class StaffLinkInvitation(Base):
    """Only pending proposals. Effective scope always lives in auth_property_assignments."""
    __tablename__ = 'staff_link_invitations'
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    staff_id: Mapped[UUID] = mapped_column(ForeignKey('staff_profiles.id'), unique=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expected_role: Mapped[str] = mapped_column(String(16))
    proposed_property_ids: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
