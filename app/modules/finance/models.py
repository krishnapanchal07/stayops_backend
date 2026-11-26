"""Financial records retain identity snapshots; money is always NUMERIC."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.security import now
from app.db.base import Base


class Audit:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    request_key: Mapped[str] = mapped_column(String(160), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))


class Invoice(Audit, Base):
    __tablename__ = 'finance_invoices'
    __table_args__ = (
        UniqueConstraint('id', 'tenant_id', 'property_id'),
        CheckConstraint('subtotal > 0 AND discount >= 0 AND late_fee >= 0 AND total > 0 AND total = subtotal - discount + late_fee', name='valid_money'),
        CheckConstraint('due_date >= issue_date', name='valid_dates'),
    )
    invoice_number: Mapped[str] = mapped_column(String(48), unique=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    billing_period: Mapped[str] = mapped_column(String(7), index=True)
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    late_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    cancelled: Mapped[bool] = mapped_column(default=False, index=True)
    items: Mapped[list['InvoiceItem']] = relationship(lazy='selectin', cascade='all, delete-orphan', order_by='InvoiceItem.position')


class InvoiceItem(Base):
    __tablename__ = 'finance_invoice_items'
    __table_args__ = (
        CheckConstraint('amount > 0', name='valid_amount'),
        CheckConstraint("type IN ('RENT','ELECTRICITY','MAINTENANCE','LATE_FEE','OTHER')", name='valid_type'),
        UniqueConstraint('invoice_id', 'position'),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey('finance_invoices.id'), index=True)
    position: Mapped[int]
    type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Payment(Audit, Base):
    __tablename__ = 'finance_payments'
    __table_args__ = (
        ForeignKeyConstraint(['invoice_id', 'tenant_id', 'property_id'], ['finance_invoices.id', 'finance_invoices.tenant_id', 'finance_invoices.property_id']),
        CheckConstraint('amount > 0', name='valid_amount'),
        CheckConstraint("status IN ('SUCCESS','PENDING','FAILED','REFUNDED')", name='valid_status'),
        CheckConstraint("method IN ('CASH','UPI','BANK_TRANSFER','CARD','OTHER')", name='valid_method'),
    )
    payment_number: Mapped[str] = mapped_column(String(48), unique=True)
    receipt_number: Mapped[str] = mapped_column(String(48), unique=True)
    invoice_id: Mapped[UUID] = mapped_column(index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey('tenants.id'), index=True)
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    payment_date: Mapped[date] = mapped_column(Date, index=True)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16), default='SUCCESS', index=True)
    reference: Mapped[str] = mapped_column(String(160), default='')
    notes: Mapped[str] = mapped_column(String(2000), default='')
    refund_reason: Mapped[str] = mapped_column(String(2000), default='')
    refunded_by: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Expense(Audit, Base):
    __tablename__ = 'finance_expenses'
    __table_args__ = (
        CheckConstraint('amount > 0', name='valid_amount'),
        CheckConstraint("status IN ('PAID','PENDING','CANCELLED')", name='valid_status'),
        CheckConstraint("category IN ('ELECTRICITY','WATER','INTERNET','MAINTENANCE','CLEANING','SALARY','SUPPLIES','REPAIRS','OTHER')", name='valid_category'),
        CheckConstraint("payment_method IN ('CASH','UPI','BANK_TRANSFER','CARD','OTHER')", name='valid_method'),
    )
    expense_number: Mapped[str] = mapped_column(String(48), unique=True)
    property_id: Mapped[str] = mapped_column(ForeignKey('properties.id'), index=True)
    property_name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(1000))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    expense_date: Mapped[date] = mapped_column(Date, index=True)
    vendor: Mapped[str] = mapped_column(String(200), default='')
    payment_method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(16), index=True)
    notes: Mapped[str] = mapped_column(String(2000), default='')
