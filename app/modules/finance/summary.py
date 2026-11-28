"""SQL aggregates; dashboard rows are deliberately bounded."""
from datetime import timedelta
from sqlalchemy import select, func
from app.modules.finance.models import Invoice, Payment, Expense
from app.modules.finance import service as s
from app.modules.finance.business_date import business_today
from app.modules.tenants.models import Tenant
from app.modules.tenants.service import permission
from fastapi import HTTPException


def overview(db, user, property_id='', from_date=None, to_date=None, tenant_id=None):
    permission(user, 'INVOICE_VIEW')
    invoices = s.invoice_query(user)
    payments = s.scope(select(Payment), Payment, user)
    expenses = s.scope(select(Expense), Expense, user) if user.role_id != 'TENANT' else select(Expense).where(False)
    if property_id:
        invoices = invoices.where(Invoice.property_id == property_id)
        payments = payments.where(Payment.property_id == property_id)
        expenses = expenses.where(Expense.property_id == property_id)
    if tenant_id:
        invoices = invoices.where(Invoice.tenant_id == tenant_id)
        payments = payments.where(Payment.tenant_id == tenant_id)
    for boundary, lower in ((from_date, True), (to_date, False)):
        if boundary:
            invoices = invoices.where(Invoice.issue_date >= boundary if lower else Invoice.issue_date <= boundary)
            payments = payments.where(Payment.payment_date >= boundary if lower else Payment.payment_date <= boundary)
            expenses = expenses.where(Expense.expense_date >= boundary if lower else Expense.expense_date <= boundary)
    iq, pq, eq = invoices.subquery(), payments.subquery(), expenses.subquery()
    def aggregate(table, column, *conditions):
        return db.scalar(select(func.coalesce(func.sum(column), 0)).select_from(table).where(*conditions))
    today = business_today()
    month, year = today.replace(day=1), today.replace(month=1, day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    next_year = year.replace(year=year.year + 1)
    billed = aggregate(iq, iq.c.total, iq.c.cancelled.is_(False))
    actual = (pq.c.status == 'SUCCESS', pq.c.source == 'MANUAL')
    test = (pq.c.status == 'SUCCESS', pq.c.source == 'RAZORPAY_TEST')
    collected = aggregate(pq, pq.c.amount, *actual)
    test_collected = aggregate(pq, pq.c.amount, *test)
    outstanding = aggregate(iq, iq.c.outstanding)
    overdue = aggregate(iq, iq.c.outstanding, iq.c.due_date < today)
    expense_total = aggregate(eq, eq.c.amount, eq.c.status == 'PAID')
    month_label = func.to_char(pq.c.payment_date, 'YYYY-MM')
    trend = db.execute(select(month_label, func.sum(pq.c.amount)).where(*actual, pq.c.payment_date >= year).group_by(month_label).order_by(month_label)).all()
    methods = db.execute(select(pq.c.method, func.sum(pq.c.amount)).where(*actual).group_by(pq.c.method)).all()
    breakdown = db.execute(select(eq.c.category, func.sum(eq.c.amount)).where(eq.c.status == 'PAID').group_by(eq.c.category)).all()
    open_rows = db.execute(invoices.where(s.outstanding > 0).order_by(Invoice.due_date, Invoice.id).limit(10)).all()
    due_rows = db.execute(invoices.where(s.outstanding > 0, Invoice.due_date == today).order_by(Invoice.id).limit(10)).all()
    recent = db.scalars(payments.order_by(Payment.created_at.desc()).limit(5)).all()
    return dict(billed=billed, collected=collected, testCollected=test_collected, outstanding=outstanding, overdue=overdue,
        overdueAccounts=db.scalar(select(func.count(func.distinct(iq.c.tenant_id))).where(iq.c.outstanding > 0, iq.c.due_date < today)),
        invoiceCount=db.scalar(select(func.count()).select_from(iq)),
        thisMonthCollected=aggregate(pq,pq.c.amount,*actual,pq.c.payment_date >= month),
        thisMonthTestCollected=aggregate(pq,pq.c.amount,*test,pq.c.payment_date >= month),
        todayCollected=aggregate(pq,pq.c.amount,*actual,pq.c.payment_date == today),
        pendingPayments=aggregate(pq,pq.c.amount,pq.c.status == 'PENDING'), expenses=expense_total,
        thisMonthExpenses=aggregate(eq,eq.c.amount,eq.c.status == 'PAID',eq.c.expense_date >= month,eq.c.expense_date < next_month),
        thisYearExpenses=aggregate(eq,eq.c.amount,eq.c.status == 'PAID',eq.c.expense_date >= year,eq.c.expense_date < next_year),
        netIncome=collected-expense_total,collectionRate=round(collected/billed*100,1) if billed else 0,
        expenseBreakdown=[dict(category=c,amount=float(a)) for c,a in breakdown],
        collectionTrend=[dict(label=c,value=float(a)) for c,a in trend],
        paymentMethods=[dict(name=c.replace('_',' ').title(),value=float(a)) for c,a in methods],
        pendingInvoiceRows=[dict(id=str(i.id),tenant=i.snapshot['tenantName'],room=i.snapshot.get('roomNumber') or '—',amount=float(balance),dueDate=str(i.due_date),status='Overdue' if i.due_date < today else 'Due') for i,p,balance,status in open_rows],
        dueToday=[dict(id=str(i.id),invoiceId=str(i.id),tenant=i.snapshot['tenantName'],room=i.snapshot.get('roomNumber') or '—',amount=float(balance),dueDate=str(i.due_date)) for i,p,balance,status in due_rows],
        recentPayments=[dict(id=str(p.id),title=p.payment_number,detail=f"{p.snapshot['tenantName']} · ₹{p.amount}"
            + (' · Razorpay Test Mode' if p.source == 'RAZORPAY_TEST' else ''),time=str(p.payment_date)) for p in recent])


def tenant_summary(db, user, tenant_id):
    permission(user,'INVOICE_VIEW')
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    if user.role_id == 'TENANT':
        stmt = stmt.where(Tenant.user_id == user.id)
    tenant = db.scalar(stmt)
    if not tenant:
        raise HTTPException(404,'Tenant not found or access denied.')
    if user.role_id not in {'TENANT','SUPER_ADMIN'}:
        s.ensure_property_access(user,tenant.property_id)
    totals = overview(db,user,tenant_id=tenant.id)
    invoices = s.invoice_query(user).where(Invoice.tenant_id == tenant.id, s.outstanding > 0)
    rows = db.execute(invoices.order_by(Invoice.due_date).limit(100)).all()
    recent = db.scalars(s.scope(select(Payment),Payment,user).where(Payment.tenant_id == tenant.id).order_by(Payment.payment_date.desc()).limit(5)).all()
    monthly = s.invoice_query(user).where(Invoice.tenant_id == tenant.id, Invoice.cancelled.is_(False)).subquery()
    charges = db.scalar(select(func.coalesce(func.sum(monthly.c.total),0)).where(monthly.c.billing_period == business_today().strftime('%Y-%m')))
    return dict(tenantId=str(tenant.id),currentDue=totals['outstanding'],overdue=totals['overdue'],totalPaid=totals['collected'],
        currentMonthCharges=float(charges),currentMonthPaid=totals['thisMonthCollected'],
        testPaid=totals['testCollected'],currentMonthTestPaid=totals['thisMonthTestCollected'],
        securityDeposit=tenant.data.get('securityDeposit') or 0,
        nextDueDate=str(rows[0][0].due_date) if rows else None,recentPayments=[s.payment_output(p) for p in recent],
        openInvoices=[s.invoice_output(r[0],r[1],r[3]) for r in rows])
