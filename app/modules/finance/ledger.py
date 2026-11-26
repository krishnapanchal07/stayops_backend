"""A paginated tenant ledger over canonical invoices and all payment installments."""
from sqlalchemy import select, literal, union_all, func, or_, and_
from app.modules.finance import service as s
from app.modules.finance.models import Invoice, Payment

LABELS={'ISSUED':'Due','OVERDUE':'Overdue','PARTIALLY_PAID':'Partially Paid','PAID':'Paid','CANCELLED':'Cancelled',
    'SUCCESS':'Paid','PENDING':'Pending','FAILED':'Failed','REFUNDED':'Refunded'}

def listing(db,user,filters):
    s.authorize(user,'invoices')
    s.authorize(user,'payments')
    iq=s.invoice_query(user).subquery()
    inv=select(iq.c.id.label('id'),literal('invoice').label('kind'),iq.c.issue_date.label('date'),
        iq.c.billing_period.label('period'),iq.c.invoice_number.label('number'),iq.c.total.label('amount'),
        iq.c.outstanding.label('outstanding'),iq.c.status.label('status'),literal('').label('method'))
    pay=s.scope(select(Payment.id,literal('payment'),Payment.payment_date,Invoice.billing_period,Payment.payment_number,
        Payment.amount,literal(0),Payment.status,Payment.method).join(Invoice,Invoice.id==Payment.invoice_id),Payment,user)
    ledger=union_all(inv,pay).subquery()
    stmt=select(ledger)
    if filters.billingPeriod:
        stmt=stmt.where(ledger.c.period==filters.billingPeriod)
    if filters.method:
        stmt=stmt.where(ledger.c.method==filters.method)
    if filters.status:
        codes=[key for key,value in LABELS.items() if value==filters.status]
        stmt=stmt.where(ledger.c.status.in_(codes))
    if filters.tab=='due':
        stmt=stmt.where(ledger.c.kind=='invoice',ledger.c.outstanding>0)
    elif filters.tab=='overdue':
        stmt=stmt.where(ledger.c.status=='OVERDUE')
    elif filters.tab=='completed':
        stmt=stmt.where(ledger.c.status.in_(['PAID','SUCCESS']))
    if filters.query:
        needle='%'+filters.query.replace('%',r'\%').replace('_',r'\_')+'%'
        # Search line descriptions without materializing all invoice items in Python.
        from app.modules.finance.models import InvoiceItem
        matching=select(InvoiceItem.invoice_id).where(InvoiceItem.description.ilike(needle))
        stmt=stmt.where(or_(ledger.c.number.ilike(needle),and_(ledger.c.kind=='invoice',ledger.c.id.in_(matching))))
    total=db.scalar(select(func.count()).select_from(stmt.subquery()))
    column={'date':ledger.c.date,'amount':ledger.c.amount,'invoiceNumber':ledger.c.number,'billingPeriod':ledger.c.period}.get(filters.sortBy,ledger.c.date)
    rows=db.execute(stmt.order_by(column.asc() if filters.sortDirection=='asc' else column.desc(),ledger.c.id).offset(filters.offset).limit(filters.limit)).all()
    invoice_ids=[r.id for r in rows if r.kind=='invoice']
    payment_ids=[r.id for r in rows if r.kind=='payment']
    invoices={r[0].id:s.invoice_output(r[0],r[1],r[3]) for r in db.execute(s.invoice_query(user).where(Invoice.id.in_(invoice_ids)))} if invoice_ids else {}
    payments={p.id:s.payment_output(p) for p in db.scalars(s.scope(select(Payment).where(Payment.id.in_(payment_ids)),Payment,user))} if payment_ids else {}
    result=[]
    for row in rows:
        invoice=invoices.get(row.id)
        payment=payments.get(row.id)
        record=invoice or payment
        result.append(dict(id=f'{row.kind}-{row.id}',date=str(row.date),billingPeriod=row.period,
            description=invoice['items'][0]['description'] if invoice else 'Payment '+payment['paymentNumber'],
            amount=invoice['outstandingAmount'] if invoice and invoice['outstandingAmount']>0 else record['total'] if invoice else payment['amount'],
            method=row.method,statusLabel=LABELS[row.status],invoiceId=invoice['id'] if invoice else payment['invoiceId'],
            invoiceNumber=record['invoiceNumber'],paymentId=payment['id'] if payment else None,payment=payment,invoice=invoice))
    return dict(items=result,total=total,offset=filters.offset,limit=filters.limit)
