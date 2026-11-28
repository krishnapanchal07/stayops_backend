"""Existing financial reports calculated in SQL, including export-ready rows."""
from decimal import Decimal
from sqlalchemy import select, func, literal, union_all
from app.core.security import now
from app.modules.finance import service as s
from app.modules.finance.models import Invoice, Payment, Expense
from app.modules.finance.summary import overview
from app.modules.tenants.service import permission


def money(value):
    whole,fraction=f'{Decimal(value):.2f}'.split('.')
    tail=whole[-3:]
    whole=whole[:-3]
    while whole:
        tail=whole[-2:]+','+tail
        whole=whole[:-2]
    return '₹'+tail+('.'+fraction if fraction!='00' else '')


def report(db,user,kind,filters):
    permission(user,'FINANCE_REPORT_VIEW')
    s.authorize(user,{'REVENUE':'invoices','PAYMENTS':'payments','OUTSTANDING':'invoices','EXPENSES':'expenses'}[kind])
    def metric(key,label,value,tone='primary'):
        return dict(id=key,label=label,value=str(value),tone=tone)
    def result(title,metrics,columns,rows,trend=None):
        return dict(type=kind,title=title,generatedAt=now().isoformat(),metrics=metrics,
            columns=[dict(id=k,header=v) for k,v in columns],rows=rows,trend=trend)
    def scoped(model,date_column):
        stmt=s.scope(select(model),model,user)
        if filters.propertyId:
            stmt=stmt.where(model.property_id==filters.propertyId)
        if filters.fromDate:
            stmt=stmt.where(date_column>=filters.fromDate)
        if filters.toDate:
            stmt=stmt.where(date_column<=filters.toDate)
        return stmt
    if kind=='REVENUE':
        totals=overview(db,user,filters.propertyId,filters.fromDate,filters.toDate)
        iq=scoped(Invoice,Invoice.issue_date).where(Invoice.cancelled.is_(False)).subquery()
        pq=scoped(Payment,Payment.payment_date).where(Payment.status=='SUCCESS',Payment.source=='MANUAL').subquery()
        im=func.to_char(iq.c.issue_date,'YYYY-MM')
        pm=func.to_char(pq.c.payment_date,'YYYY-MM')
        combined=union_all(select(im.label('month'),func.sum(iq.c.total).label('billed'),literal(0).label('collected')).group_by(im),
            select(pm,literal(0),func.sum(pq.c.amount)).group_by(pm)).subquery()
        groups=db.execute(select(combined.c.month,func.sum(combined.c.billed),func.sum(combined.c.collected)).group_by(combined.c.month).order_by(combined.c.month)).all()
        return result('Revenue Report',[metric(k,label,money(totals[k])) for k,label in [('billed','Total Invoiced'),('collected','Actual Collections'),('testCollected','Test Mode Applied'),('outstanding','Outstanding'),('overdue','Overdue')]],
            [('month','Month'),('billed','Invoiced'),('collected','Actual Collections')],
            [dict(id=m,label=m,values=dict(month=m,billed=money(b),collected=money(c))) for m,b,c in groups],
            [dict(label=m,value=float(c)) for m,b,c in groups])
    if kind=='PAYMENTS':
        stmt=scoped(Payment,Payment.payment_date)
        if filters.status:
            stmt=stmt.where(Payment.status==filters.status)
        table=stmt.subquery()
        counts=dict(db.execute(select(table.c.status,func.count()).group_by(table.c.status)).all())
        groups=db.execute(select(table.c.source,table.c.method,func.count(),func.sum(table.c.amount)).where(table.c.status=='SUCCESS').group_by(table.c.source,table.c.method)).all()
        return result('Payment Report',[metric('total','Total Payments',sum(counts.values()))]+[metric(k.lower(),label,counts.get(k,0)) for k,label in [('SUCCESS','Successful'),('PENDING','Pending'),('FAILED','Failed'),('REFUNDED','Refunded')]],
            [('source','Source'),('method','Method'),('count','Successful count'),('amount','Amount')],
            [dict(id=f'{source}-{m}',label=m.replace('_',' ').title(),values=dict(source='Razorpay Test Mode' if source=='RAZORPAY_TEST' else 'Manual',method=m.replace('_',' ').title(),count=c,amount=money(a))) for source,m,c,a in groups])
    if kind=='EXPENSES':
        stmt=scoped(Expense,Expense.expense_date).where(Expense.status!='CANCELLED')
        if filters.category:
            stmt=stmt.where(Expense.category==filters.category)
        table=stmt.subquery()
        total,count=db.execute(select(func.coalesce(func.sum(table.c.amount),0),func.count())).one()
        groups=db.execute(select(table.c.category,func.count(),func.sum(table.c.amount)).group_by(table.c.category)).all()
        return result('Expense Report',[metric('total','Total Expenses',money(total)),metric('count','Expense records',count)],
            [('category','Category'),('count','Count'),('amount','Amount')],
            [dict(id=k,label=k.title(),values=dict(category=k.title(),count=c,amount=money(a))) for k,c,a in groups])
    stmt=s.invoice_query(user).where(s.outstanding>0)
    if filters.propertyId:
        stmt=stmt.where(Invoice.property_id==filters.propertyId)
    if filters.fromDate:
        stmt=stmt.where(Invoice.due_date>=filters.fromDate)
    if filters.toDate:
        stmt=stmt.where(Invoice.due_date<=filters.toDate)
    table=stmt.subquery()
    count,total=db.execute(select(func.count(),func.coalesce(func.sum(table.c.outstanding),0))).one()
    overdue_count,overdue_amount=db.execute(select(func.count(),func.coalesce(func.sum(table.c.outstanding),0)).where(table.c.status=='OVERDUE')).one()
    rows=db.execute(stmt.order_by(Invoice.due_date,Invoice.id).limit(40)).all()
    return result('Outstanding Dues Report',[metric('accounts','Open invoices',count),metric('amount','Outstanding',money(total)),metric('overdue','Overdue invoices',overdue_count),metric('overdueAmt','Overdue amount',money(overdue_amount))],
        [('tenant','Tenant'),('property','Property'),('due','Due date'),('amount','Outstanding'),('status','Status')],
        [dict(id=str(i.id),label=i.snapshot['tenantName'],values=dict(tenant=i.snapshot['tenantName'],property=i.snapshot['propertyName'],due=str(i.due_date),amount=money(balance),status=status)) for i,p,balance,status in rows])
