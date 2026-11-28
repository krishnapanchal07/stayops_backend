from datetime import date
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.modules.auth.dependencies import get_active_user, require_trusted_origin
from app.modules.finance import service as s
from app.modules.finance.summary import overview, tenant_summary
from app.modules.finance.models import Invoice, Payment, Expense
from app.modules.finance.razorpay_router import router as razorpay_router
from app.modules.finance.schemas import InvoiceWrite, PaymentWrite, ExpenseWrite, Refund
from app.modules.tenants.models import Tenant
from app.modules.tenants.service import output as tenant_output, permission
from app.modules.properties.models import Property
from app.core.security import now

router = APIRouter(prefix='/finance', tags=['Finance'])
router.include_router(razorpay_router)
write = [Depends(require_trusted_origin)]

class Filters(BaseModel):
    tab: Literal['all','due','overdue','completed'] = 'all'
    query: str = Field('',max_length=120)
    propertyId: str = Field('',max_length=64)
    tenantId: UUID | None = None
    invoiceId: UUID | None = None
    status: str = Field('',max_length=24)
    billingPeriod: str = Field('',max_length=7)
    dueStatus: Literal['','DUE','OVERDUE','PAID'] = ''
    method: str = Field('',max_length=24)
    source: Literal['','MANUAL','RAZORPAY_TEST'] = ''
    category: str = Field('',max_length=24)
    fromDate: date | None = None
    toDate: date | None = None
    offset: int = Field(0,ge=0)
    limit: int = Field(20,ge=1,le=100)
    sortBy: str = Field('createdAt',max_length=32)
    sortDirection: Literal['asc','desc'] = 'desc'

@router.get('/ledger')
def ledger(filters:Filters=Depends(),db:Session=Depends(get_db),user=Depends(get_active_user)):
    from app.modules.finance.ledger import listing
    return listing(db,user,filters)

@router.get('/reports/{kind}')
def financial_report(kind:Literal['REVENUE','PAYMENTS','OUTSTANDING','EXPENSES'],filters:Filters=Depends(),db:Session=Depends(get_db),user=Depends(get_active_user)):
    from app.modules.finance.reports import report
    return report(db,user,kind,filters)

@router.get('/overview')
def finance_overview(db:Session=Depends(get_db),user=Depends(get_active_user),propertyId:str=Query('',max_length=64),fromDate:date|None=None,toDate:date|None=None):
    return overview(db,user,propertyId,fromDate,toDate)

@router.get('/options/properties')
def properties(db:Session=Depends(get_db),user=Depends(get_active_user)):
    permission(user,'INVOICE_VIEW')
    from app.modules.properties.service import scoped
    return [dict(id=p.id,name=p.name) for p in db.scalars(scoped(select(Property),user).order_by(Property.name))]

@router.get('/options/tenants')
def tenants(db:Session=Depends(get_db),user=Depends(get_active_user),propertyId:str=Query('',max_length=64),query:str=Query('',max_length=120),offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=100)):
    permission(user,'INVOICE_VIEW')
    if user.role_id == 'TENANT':
        stmt=select(Tenant).where(Tenant.user_id == user.id)
    else:
        stmt=s.scope(select(Tenant),Tenant,user)
    if propertyId:
        stmt=stmt.where(Tenant.property_id == propertyId)
    if query:
        stmt=stmt.where(func.lower(Tenant.data['firstName'].as_string() + ' ' + Tenant.data['lastName'].as_string()).contains(query.lower(),autoescape=True))
    total=db.scalar(select(func.count()).select_from(stmt.subquery()))
    return dict(items=[tenant_output(t) for t in db.scalars(stmt.order_by(Tenant.tenant_code).offset(offset).limit(limit))],total=total,offset=offset,limit=limit)

@router.get('/tenants/me/summary')
def my_summary(db:Session=Depends(get_db),user=Depends(get_active_user)):
    tenant=db.scalar(select(Tenant).where(Tenant.user_id == user.id))
    if not tenant:
        raise HTTPException(404,'No linked tenant profile.')
    return tenant_summary(db,user,tenant.id)

@router.get('/tenants/{tenant_id}/summary')
def tenant_totals(tenant_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    return tenant_summary(db,user,tenant_id)

@router.get('/invoices')
def invoices(filters:Filters=Depends(),db:Session=Depends(get_db),user=Depends(get_active_user)):
    return s.listing(db,user,'invoices',filters)

@router.get('/payments')
def payments(filters:Filters=Depends(),db:Session=Depends(get_db),user=Depends(get_active_user)):
    return s.listing(db,user,'payments',filters)

@router.get('/expenses')
def expenses(filters:Filters=Depends(),db:Session=Depends(get_db),user=Depends(get_active_user)):
    return s.listing(db,user,'expenses',filters)

@router.get('/invoices/summary')
def invoice_totals(db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'invoices')
    v=overview(db,user)
    return {k:v[k] for k in ('billed','collected','testCollected','outstanding','overdue')} | {'count':v['invoiceCount']}

@router.get('/payments/summary')
def payment_totals(db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'payments')
    v=overview(db,user)
    return dict(collected=v['collected'],testCollected=v['testCollected'],pending=v['pendingPayments'],overdue=v['overdue'],todayCollection=v['todayCollected'])

@router.get('/expenses/summary')
def expense_totals(db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'expenses')
    v=overview(db,user)
    return dict(total=v['expenses'],thisMonth=v['thisMonthExpenses'],thisYear=v['thisYearExpenses'],breakdown=v['expenseBreakdown'])

@router.post('/invoices',dependencies=write)
def create_invoice(values:InvoiceWrite,db:Session=Depends(get_db),user=Depends(get_active_user),key:str|None=Header(None,alias='Idempotency-Key')):
    return s.invoice_detail(db,s.write_invoice(db,values,user,key))

@router.get('/invoices/{invoice_id}')
def invoice_detail(invoice_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'invoices')
    return s.invoice_detail(db,s.get(db,Invoice,invoice_id,user))

@router.patch('/invoices/{invoice_id}',dependencies=write)
def edit_invoice(invoice_id:UUID,values:InvoiceWrite,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'invoices',True)
    return s.invoice_detail(db,s.write_invoice(db,values,user,item=s.get(db,Invoice,invoice_id,user,True)))

@router.post('/invoices/{invoice_id}/cancel',dependencies=write)
def cancel_invoice(invoice_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'invoices',True)
    item=s.get(db,Invoice,invoice_id,user,True)
    if db.scalar(select(func.count()).select_from(Payment).where(Payment.invoice_id == item.id)):
        raise HTTPException(409,'Invoices with payment history cannot be cancelled.')
    item.cancelled=True
    db.commit()
    return s.invoice_detail(db,item)

@router.post('/payments',dependencies=write)
def record_payment(values:PaymentWrite,db:Session=Depends(get_db),user=Depends(get_active_user),key:str|None=Header(None,alias='Idempotency-Key')):
    return s.payment_output(s.record_payment(db,values,user,key))

@router.get('/payments/{payment_id}')
@router.get('/payments/{payment_id}/receipt')
def payment_detail(payment_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'payments')
    return s.payment_output(s.get(db,Payment,payment_id,user))

@router.post('/payments/{payment_id}/refund',dependencies=write)
def refund(payment_id:UUID,values:Refund,db:Session=Depends(get_db),user=Depends(get_active_user)):
    permission(user,'PAYMENT_REFUND')
    if user.role_id == 'TENANT':
        raise HTTPException(403,'Staff account required.')
    item=s.get(db,Payment,payment_id,user)
    s.get(db,Invoice,item.invoice_id,user,True)
    db.refresh(item,with_for_update=True)
    if item.source == 'RAZORPAY_TEST':
        raise HTTPException(409,'Razorpay Test Mode receipts cannot be marked as external refunds.')
    if item.status != 'SUCCESS':
        raise HTTPException(409,'Only successful payments can be marked refunded.')
    item.status='REFUNDED'
    item.refund_reason, item.refunded_by, item.refunded_at=values.reason,user.id,now()
    db.commit()
    return s.payment_output(item)

@router.post('/expenses',dependencies=write)
def create_expense(values:ExpenseWrite,db:Session=Depends(get_db),user=Depends(get_active_user),key:str|None=Header(None,alias='Idempotency-Key')):
    return s.expense_output(s.write_expense(db,values,user,key))

@router.get('/expenses/{expense_id}')
def expense_detail(expense_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'expenses')
    return s.expense_output(s.get(db,Expense,expense_id,user))

@router.patch('/expenses/{expense_id}',dependencies=write)
def edit_expense(expense_id:UUID,values:ExpenseWrite,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'expenses',True)
    return s.expense_output(s.write_expense(db,values,user,item=s.get(db,Expense,expense_id,user,True)))

@router.post('/expenses/{expense_id}/cancel',dependencies=write)
def cancel_expense(expense_id:UUID,db:Session=Depends(get_db),user=Depends(get_active_user)):
    s.authorize(user,'expenses',True)
    item=s.get(db,Expense,expense_id,user,True)
    item.status='CANCELLED'
    db.commit()
    return s.expense_output(item)
