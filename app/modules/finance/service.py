"""Transactional finance operations and scoped SQL read models."""
import hashlib
from decimal import Decimal
from uuid import UUID, uuid4
from fastapi import HTTPException
from sqlalchemy import select, func, case, or_, text
from app.core.security import now
from app.modules.auth.dependencies import trusted_property_ids, ensure_property_access
from app.modules.tenants.models import Tenant
from app.modules.tenants.service import permission
from app.modules.properties.models import Property
from app.modules.finance.models import Invoice, InvoiceItem, Payment, Expense
from app.modules.finance.business_date import business_today


def authorize(user, kind, write=False):
    permission(user, {'invoices':'INVOICE', 'payments':'PAYMENT', 'expenses':'EXPENSE'}[kind] + ('_CREATE' if write else '_VIEW'))
    if write and user.role_id == 'TENANT':
        raise HTTPException(403, 'Payments must be recorded by authorized staff. Contact your property office.')


def scope(stmt, model, user):
    if user.role_id == 'SUPER_ADMIN':
        return stmt
    if user.role_id == 'TENANT':
        if model is Expense:
            raise HTTPException(403, 'Expenses are not available to tenants.')
        return stmt.where(model.tenant_id.in_(select(Tenant.id).where(Tenant.user_id == user.id)))
    return stmt.where(model.property_id.in_(trusted_property_ids(user)))


def get(db, model, key, user, lock=False):
    stmt = scope(select(model).where(model.id == key), model, user)
    if lock:
        stmt = stmt.with_for_update()
    result = db.scalar(stmt.execution_options(populate_existing=True))
    if not result:
        raise HTTPException(404, 'Record not found or access denied.')
    return result


def replay(db, model, user, key, values):
    if not key or len(key) > 100:
        raise HTTPException(422, 'A valid Idempotency-Key is required.')
    request_key = f'{user.id}:{key}'
    digest = hashlib.sha256(values.model_dump_json().encode()).hexdigest()
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': model.__tablename__ + request_key})
    existing = db.scalar(select(model).where(model.request_key == request_key))
    if existing and existing.request_digest != digest:
        raise HTTPException(409, 'This submission key was already used with different values.')
    if existing:
        get(db, model, existing.id, user)
    return existing, request_key, digest


paid_query = select(Payment.invoice_id, func.sum(Payment.amount).label('paid')).where(Payment.status == 'SUCCESS').group_by(Payment.invoice_id).subquery()
paid = func.coalesce(paid_query.c.paid, 0)
outstanding = case((Invoice.cancelled, 0), else_=Invoice.total - paid)


def invoice_status():
    return case((Invoice.cancelled, 'CANCELLED'), (paid >= Invoice.total, 'PAID'),
        (Invoice.due_date < business_today(), 'OVERDUE'), (paid > 0, 'PARTIALLY_PAID'), else_='ISSUED')


def invoice_query(user):
    status = invoice_status()
    return scope(select(Invoice, paid.label('paid'), outstanding.label('outstanding'), status.label('status')).outerjoin(paid_query, paid_query.c.invoice_id == Invoice.id), Invoice, user)


def invoice_output(item, received=Decimal(0), status=None):
    balance = Decimal(0) if item.cancelled else item.total - received
    status = status or ('CANCELLED' if item.cancelled else 'PAID' if balance == 0 else 'OVERDUE' if item.due_date < business_today() else 'PARTIALLY_PAID' if received else 'ISSUED')
    return dict(id=str(item.id), invoiceNumber=item.invoice_number, tenantId=str(item.tenant_id), propertyId=item.property_id,
        **item.snapshot, billingPeriod=item.billing_period, issueDate=str(item.issue_date), dueDate=str(item.due_date),
        items=[dict(id=str(i.id), type=i.type, description=i.description, amount=float(i.amount)) for i in item.items],
        discount=float(item.discount), lateFee=float(item.late_fee), subtotal=float(item.subtotal), total=float(item.total),
        paidAmount=float(received), outstandingAmount=float(balance), status=status, cancelled=item.cancelled,
        createdAt=item.created_at.isoformat(), updatedAt=item.updated_at.isoformat())


def invoice_detail(db, item):
    return invoice_output(item, invoice_received(db, item.id))


def invoice_received(db, invoice_id):
    return db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.invoice_id == invoice_id, Payment.status == 'SUCCESS'))


def invoice_balance(db, invoice):
    return Decimal(0) if invoice.cancelled else invoice.total - invoice_received(db, invoice.id)


def payment_output(item):
    return dict(id=str(item.id), paymentNumber=item.payment_number, receiptNumber=item.receipt_number, invoiceId=str(item.invoice_id),
        tenantId=str(item.tenant_id), propertyId=item.property_id, **item.snapshot, amount=float(item.amount),
        paymentDate=str(item.payment_date), method=item.method, status=item.status, reference=item.reference, notes=item.notes,
        refundReason=item.refund_reason, createdAt=item.created_at.isoformat(),
        source=item.source, isTest=item.source == 'RAZORPAY_TEST')


def expense_output(item):
    return dict(id=str(item.id), expenseNumber=item.expense_number, propertyId=item.property_id, propertyName=item.property_name,
        category=item.category, description=item.description, amount=float(item.amount), expenseDate=str(item.expense_date),
        vendor=item.vendor, paymentMethod=item.payment_method, status=item.status, notes=item.notes,
        createdAt=item.created_at.isoformat(), createdBy=str(item.created_by))


def write_invoice(db, values, user, key=None, item=None):
    authorize(user, 'invoices', True)
    if item:
        if db.scalar(select(func.count()).select_from(Payment).where(Payment.invoice_id == item.id)):
            raise HTTPException(409, 'Invoices with recorded payments cannot be edited or cancelled.')
        if item.cancelled or item.tenant_id != values.tenantId:
            raise HTTPException(409, 'Cancelled invoices and invoice tenant identities cannot be changed.')
    else:
        existing, request_key, digest = replay(db, Invoice, user, key, values)
        if existing:
            return existing
    tenant = db.get(Tenant, values.tenantId)
    if not tenant or not tenant.property_id:
        raise HTTPException(422, 'Select an existing tenant assigned to a property.')
    property_id = item.property_id if item else tenant.property_id
    ensure_property_access(user, property_id)
    if values.propertyId and values.propertyId != property_id:
        raise HTTPException(422, 'Tenant does not belong to the selected property.')
    subtotal = sum((i.amount for i in values.items), Decimal(0))
    total = subtotal - values.discount + values.lateFee
    if total <= 0 or subtotal > Decimal('9999999999.99') or total > Decimal('9999999999.99'):
        raise HTTPException(422, 'Invoice total must be positive and within the supported amount range.')
    if not item:
        prop = db.get(Property, property_id)
        snapshot = dict(tenantName=f"{tenant.data['firstName']} {tenant.data['lastName']}", tenantCode=tenant.tenant_code,
            propertyName=prop.name, roomId=tenant.room_id, roomNumber=tenant.data.get('roomNumber'), bedLabel=tenant.data.get('bedLabel'))
        item = Invoice(invoice_number='INV-' + uuid4().hex.upper(), tenant_id=tenant.id, property_id=property_id,
            snapshot=snapshot, created_by=user.id, request_key=request_key, request_digest=digest)
        db.add(item)
    item.billing_period, item.issue_date, item.due_date = values.billingPeriod, values.issueDate, values.dueDate
    item.subtotal, item.discount, item.late_fee, item.total = subtotal, values.discount, values.lateFee, total
    # Flush removal before replacing positions, to preserve the unique line-position constraint.
    item.items.clear()
    if item.id:
        db.flush()
    item.items = [InvoiceItem(position=n, type=i.type, description=i.description, amount=i.amount) for n, i in enumerate(values.items)]
    db.commit()
    return item


def record_payment(db, values, user, key):
    authorize(user, 'payments', True)
    existing, request_key, digest = replay(db, Payment, user, key, values)
    if existing:
        return existing
    invoice = get(db, Invoice, values.invoiceId, user, lock=True)
    if invoice.tenant_id != values.tenantId:
        raise HTTPException(422, 'Invoice does not belong to this tenant.')
    if values.amount > invoice_balance(db, invoice):
        raise HTTPException(409, 'Payment exceeds the outstanding balance or invoice is cancelled.')
    if values.paymentDate < invoice.issue_date:
        raise HTTPException(422, 'Payment cannot precede the invoice issue date.')
    if values.reference:
        # Also serialize reuse of a transaction reference with a new submission key.
        lock_key = f'payment-reference:{invoice.property_id}:{values.method}:{values.reference.casefold()}'
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': lock_key})
        duplicate = db.scalar(select(Payment.id).where(Payment.property_id == invoice.property_id,
            Payment.source == 'MANUAL', Payment.method == values.method, func.lower(Payment.reference) == values.reference.lower()))
        if duplicate:
            raise HTTPException(409, 'This transaction reference has already been recorded.')
    payment = add_successful_payment(db, invoice, user.id, values.amount, values.paymentDate,
        values.method, values.reference, values.notes, request_key, digest)
    db.commit()
    return payment


def add_successful_payment(db, invoice, actor_id, amount, payment_date, method, reference,
                           notes, request_key, digest, *, source='MANUAL'):
    """Create a ledger entry after the caller locks the invoice and validates the balance."""
    token = uuid4().hex.upper()
    snapshot = {k:v for k,v in invoice.snapshot.items() if k in {'tenantName','propertyName','roomNumber'}}
    payment = Payment(payment_number='PAY-' + token, receipt_number='RCT-' + token, invoice_id=invoice.id,
        tenant_id=invoice.tenant_id, property_id=invoice.property_id, snapshot=snapshot | {'invoiceNumber':invoice.invoice_number},
        amount=amount, payment_date=payment_date, method=method, reference=reference,
        notes=notes, created_by=actor_id, request_key=request_key, request_digest=digest, source=source)
    db.add(payment)
    return payment


def write_expense(db, values, user, key=None, item=None):
    authorize(user, 'expenses', True)
    if not item:
        existing, request_key, digest = replay(db, Expense, user, key, values)
        if existing:
            return existing
    prop = db.get(Property, values.propertyId)
    if not prop:
        raise HTTPException(422, 'Property does not exist.')
    ensure_property_access(user, prop.id)
    if not item:
        item = Expense(expense_number='EXP-' + uuid4().hex.upper(), created_by=user.id, request_key=request_key, request_digest=digest)
        db.add(item)
    item.property_id, item.property_name = prop.id, prop.name
    for source, target in [('category','category'),('description','description'),('amount','amount'),('expenseDate','expense_date'),
        ('vendor','vendor'),('paymentMethod','payment_method'),('status','status'),('notes','notes')]:
        setattr(item, target, getattr(values, source))
    db.commit()
    return item


def listing(db, user, kind, filters):
    authorize(user, kind)
    model = {'invoices':Invoice, 'payments':Payment, 'expenses':Expense}[kind]
    stmt = invoice_query(user) if model is Invoice else scope(select(model), model, user)
    if filters.propertyId:
        stmt = stmt.where(model.property_id == filters.propertyId)
    if filters.tenantId and model is not Expense:
        stmt = stmt.where(model.tenant_id == filters.tenantId)
    if filters.invoiceId and model is Payment:
        stmt = stmt.where(Payment.invoice_id == filters.invoiceId)
    if filters.status:
        if model is Invoice:
            stmt = stmt.where(invoice_status() == filters.status)
        else:
            stmt = stmt.where(model.status == filters.status)
    if filters.billingPeriod and model is not Expense:
        stmt = stmt.where(Invoice.billing_period == filters.billingPeriod) if model is Invoice else stmt.where(Payment.invoice_id.in_(select(Invoice.id).where(Invoice.billing_period == filters.billingPeriod)))
    if model is Invoice and filters.dueStatus:
        stmt = stmt.where(outstanding > 0) if filters.dueStatus in {'DUE','OVERDUE'} else stmt.where(paid >= Invoice.total, Invoice.cancelled.is_(False))
        if filters.dueStatus == 'OVERDUE':
            stmt = stmt.where(Invoice.due_date < business_today())
    if model is Payment and filters.method:
        stmt = stmt.where(Payment.method == filters.method)
    if model is Payment and filters.source:
        stmt = stmt.where(Payment.source == filters.source)
    if model is Expense and filters.category:
        stmt = stmt.where(Expense.category == filters.category)
    date_column = {Invoice:Invoice.issue_date, Payment:Payment.payment_date, Expense:Expense.expense_date}[model]
    if filters.fromDate:
        stmt = stmt.where(date_column >= filters.fromDate)
    if filters.toDate:
        stmt = stmt.where(date_column <= filters.toDate)
    if filters.query:
        needle = '%' + filters.query.replace('%',r'\%').replace('_',r'\_') + '%'
        columns = [Invoice.invoice_number, Invoice.snapshot['tenantName'].as_string(), Invoice.snapshot['tenantCode'].as_string(), Invoice.snapshot['roomNumber'].as_string()] if model is Invoice else [Payment.payment_number, Payment.snapshot['tenantName'].as_string(), Payment.snapshot['invoiceNumber'].as_string(), Payment.reference] if model is Payment else [Expense.expense_number, Expense.description, Expense.vendor, Expense.property_name]
        stmt = stmt.where(or_(*(c.ilike(needle) for c in columns)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    columns = {'createdAt': model.created_at, 'propertyName': Invoice.snapshot['propertyName'].as_string() if model is Invoice else Payment.snapshot['propertyName'].as_string() if model is Payment else Expense.property_name}
    if model is Invoice:
        columns |= {'invoiceNumber':Invoice.invoice_number,'tenantName':Invoice.snapshot['tenantName'].as_string(), 'total':Invoice.total,'dueDate':Invoice.due_date,'billingPeriod':Invoice.billing_period,'outstandingAmount':outstanding}
    elif model is Payment:
        columns |= {'paymentNumber':Payment.payment_number,'tenantName':Payment.snapshot['tenantName'].as_string(),'invoiceNumber':Payment.snapshot['invoiceNumber'].as_string(),'amount':Payment.amount,'paymentDate':Payment.payment_date}
    else:
        columns |= {'expenseNumber':Expense.expense_number,'description':Expense.description,'amount':Expense.amount,'expenseDate':Expense.expense_date,'category':Expense.category}
    column = columns.get(filters.sortBy, model.created_at)
    stmt = stmt.order_by(column.asc() if filters.sortDirection == 'asc' else column.desc(), model.id).offset(filters.offset).limit(filters.limit)
    rows = db.execute(stmt).all()
    items = [invoice_output(row[0], row[1], row[3]) if model is Invoice else payment_output(row[0]) if model is Payment else expense_output(row[0]) for row in rows]
    return dict(items=items, total=total, offset=filters.offset, limit=filters.limit)
