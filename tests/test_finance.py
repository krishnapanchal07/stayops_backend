"""Finance regressions against the isolated PostgreSQL fixture, never the application DB."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select, func
from conftest import sign_in, authorization
from app.modules.auth.models import User, PropertyAssignment
from app.modules.finance.models import Invoice, Payment, Expense
from app.modules.finance.schemas import PaymentWrite
from app.modules.finance import service
from app.modules.finance.business_date import business_today
from app.modules.tenants.models import Tenant
from app.modules.properties.models import Property


@pytest.fixture
def finance(setup):
    client,factory,*_=setup
    headers=authorization(sign_in(client,'admin@example.com'))
    with factory() as db:
        tenant=Tenant(email='finance@example.com',property_id='prop-sunrise',status='ONBOARDING',source='DIRECT',
            data={'firstName':'Finance','lastName':'QA','phone':'9876543210','roomRent':10000,'securityDeposit':5000})
        db.add(tenant)
        db.commit()
        tenant_id=str(tenant.id)
    today=str(business_today())
    invoice=dict(tenantId=tenant_id,billingPeriod=today[:7],issueDate=today,dueDate=today,
        items=[dict(id='rent',type='RENT',description='Rent',amount=10000)],discount=0,lateFee=0)
    return client,factory,headers,invoice


def post(client,headers,path,payload,key=None):
    return client.post('/api/v1/finance/'+path,json=payload,headers=headers|{'Idempotency-Key':key or str(uuid4())})


def test_invoice_payment_receipt_and_refund(finance):
    client,factory,h,values=finance
    response=post(client,h,'invoices',values,'invoice-retry')
    assert response.status_code==200,response.text
    invoice=response.json()
    assert invoice['total']==10000 and invoice['status']=='ISSUED'
    assert post(client,h,'invoices',values,'invoice-retry').json()['id']==invoice['id']
    assert post(client,h,'invoices',values|{'discount':1},'invoice-retry').status_code==409
    payment=dict(tenantId=values['tenantId'],invoiceId=invoice['id'],amount=4000,method='CASH',paymentDate=str(business_today()),reference='',notes='Partial')
    response=post(client,h,'payments',payment,'payment-retry')
    assert response.status_code==200,response.text
    saved=response.json()
    assert post(client,h,'payments',payment,'payment-retry').json()['id']==saved['id']
    path='/api/v1/finance/invoices/'+invoice['id']
    live=client.get(path,headers=h).json()
    assert (live['status'],live['paidAmount'],live['outstandingAmount'])==('PARTIALLY_PAID',4000,6000)
    assert client.patch(path,json=values,headers=h).status_code==409
    assert client.post(path+'/cancel',headers=h).status_code==409
    assert post(client,h,'payments',payment|{'amount':6001}).status_code==409
    assert post(client,h,'payments',payment|{'amount':6000}).status_code==200
    assert client.get(path,headers=h).json()['status']=='PAID'
    receipt=client.get('/api/v1/finance/payments/'+saved['id']+'/receipt',headers=h)
    assert receipt.json()['receiptNumber']==saved['receiptNumber']
    assert receipt.json()['amount']==4000
    response=client.post('/api/v1/finance/payments/'+saved['id']+'/refund',json={'reason':'Recorded return of cash'},headers=h)
    assert response.status_code==200,response.text
    assert client.get(path,headers=h).json()['outstandingAmount']==4000
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Payment))==2
        item=db.get(Invoice,UUID(invoice['id']))
        assert item.total==Decimal('10000.00')


@pytest.mark.parametrize('patch',[{'tenantId':str(UUID(int=1))},{'propertyId':'missing'}, {'discount':10001},{'dueDate':'2000-01-01'}, {'items':[]}, {'items':[{'type':'RENT','description':'Bad','amount':-1}]}, {'items':[{'type':'RENT','description':'Bad','amount':'1.001'}]}])
def test_invalid_invoice(finance,patch):
    c,f,h,v=finance
    assert post(c,h,'invoices',v|patch).status_code==422
    with f() as db:
        assert db.scalar(select(func.count()).select_from(Invoice))==0


def test_expense_crud_search_and_aggregates(finance):
    c,f,h,v=finance
    values=dict(propertyId='prop-sunrise',category='REPAIRS',description='QA repairs',amount=100.25,expenseDate=str(business_today()),vendor='Vendor',paymentMethod='CASH',status='PAID',notes='')
    response=post(c,h,'expenses',values,'expense-retry')
    assert response.status_code==200,response.text
    item=response.json()
    assert post(c,h,'expenses',values,'expense-retry').json()['id']==item['id']
    assert post(c,h,'expenses',values|{'propertyId':'absent'}).status_code==422
    assert post(c,h,'expenses',values|{'amount':0}).status_code==422
    path='/api/v1/finance/expenses/'+item['id']
    assert c.patch(path,json=values|{'amount':200.50},headers=h).json()['amount']==200.5
    assert c.get('/api/v1/finance/expenses?query=repairs&limit=1',headers=h).json()['total']==1
    assert c.get('/api/v1/finance/expenses/summary',headers=h).json()['total']==200.5
    assert c.post(path+'/cancel',headers=h).status_code==200
    assert c.get('/api/v1/finance/expenses/summary',headers=h).json()['total']==0
    with f() as db:
        assert db.get(Expense,UUID(item['id'])).status=='CANCELLED'


def test_expense_summary_excludes_later_periods(finance):
    c, _, headers, _ = finance
    today = business_today()
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    next_year = today.replace(year=today.year + 1, month=1, day=1)
    expense = dict(propertyId='prop-sunrise', category='REPAIRS', description='QA repairs',
        paymentMethod='CASH', status='PAID')
    for when, amount in [(today, 10), (next_month, 20), (next_year, 30)]:
        result = post(c, headers, 'expenses', expense | {'expenseDate': str(when), 'amount': amount})
        assert result.status_code == 200, result.text

    totals = c.get('/api/v1/finance/expenses/summary', headers=headers).json()
    assert totals['total'] == 60
    assert totals['thisMonth'] == 10
    assert totals['thisYear'] == (30 if next_month.year == today.year else 10)


def test_role_and_tenant_scope(finance):
    c,f,h,v=finance
    invoice=post(c,h,'invoices',v).json()
    payment=post(c,h,'payments',dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=10,method='CASH',paymentDate=str(business_today()))).json()
    owner=authorization(sign_in(c,'owner@example.com'))
    assert c.get('/api/v1/finance/invoices',headers=owner).json()['total']==0
    assert c.get('/api/v1/finance/invoices/'+invoice['id'],headers=owner).status_code==404
    assert c.get('/api/v1/finance/payments/'+payment['id']+'/receipt',headers=owner).status_code==404
    tenant_headers=authorization(sign_in(c))
    assert c.get('/api/v1/finance/invoices',headers=tenant_headers).json()['total']==0
    assert c.get('/api/v1/finance/invoices/'+invoice['id'],headers=tenant_headers).status_code==404
    with f() as db:
        tenant=db.get(Tenant,UUID(v['tenantId']))
        tenant.user_id=db.scalar(select(User.id).where(User.email=='tenant@example.com'))
        owner_user=db.scalar(select(User).where(User.email=='owner@example.com'))
        db.get(Property,'prop-sunrise').owner_user_id=owner_user.id
        db.commit()
    assert c.get('/api/v1/finance/invoices',headers=tenant_headers).json()['total']==1
    assert c.get('/api/v1/finance/tenants/me/summary',headers=tenant_headers).json()['currentDue']==9990
    assert c.get('/api/v1/finance/payments/'+payment['id']+'/receipt',headers=tenant_headers).status_code==200
    assert post(c,tenant_headers,'payments',dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=10,method='CASH',paymentDate=str(business_today()))).status_code==403
    assert c.get('/api/v1/finance/invoices',headers=owner).json()['total']==1
    with f() as db:
        user=db.scalar(select(User).where(User.email=='owner@example.com'))
        user.role_id='SECURITY'
        db.commit()
    assert c.get('/api/v1/finance/invoices',headers=owner).status_code==403


def test_concurrent_payments_and_history(finance):
    c,f,h,v=finance
    invoice=post(c,h,'invoices',v).json()
    with f() as db:
        user_id=db.scalar(select(User.id).where(User.email=='admin@example.com'))
    def pay(_):
        with f() as db:
            try:
                user=db.get(User,user_id)
                service.record_payment(db,PaymentWrite(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=6000,method='CASH',paymentDate=business_today()),user,str(uuid4()))
                return 200
            except Exception as e:
                db.rollback()
                return getattr(e,'status_code',500)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(pay,range(2)))==[200,409]
    with f() as db:
        tenant=db.get(Tenant,UUID(v['tenantId']))
        tenant.status='CHECKED_OUT'
        tenant.room_id=tenant.bed_id=None
        tenant.data=tenant.data|{'roomRent':99999}
        db.commit()
    live=c.get('/api/v1/finance/invoices/'+invoice['id'],headers=h).json()
    assert live['total']==10000 and live['outstandingAmount']==4000
    totals=c.get('/api/v1/finance/overview',headers=h).json()
    assert totals['collected']==6000 and totals['outstanding']==4000


def test_invoice_edit_cancel_and_pagination(finance):
    c,f,h,v=finance
    ids=[post(c,h,'invoices',v).json()['id'] for _ in range(3)]
    assert len(set(ids))==3
    response=c.patch('/api/v1/finance/invoices/'+ids[0],json=v|{'discount':123.45,'lateFee':1},headers=h)
    assert response.status_code==200,response.text
    assert response.json()['total']==9877.55
    page=c.get('/api/v1/finance/invoices?limit=1&offset=1&query=Finance&sortBy=total',headers=h).json()
    assert page['total']==3 and len(page['items'])==1
    assert c.post('/api/v1/finance/invoices/'+ids[0]+'/cancel',headers=h).json()['status']=='CANCELLED'
    assert c.get('/api/v1/finance/invoices?status=CANCELLED',headers=h).json()['total']==1


def test_ledger_contains_every_installment_and_is_scoped(finance):
    c,f,h,v=finance
    invoice=post(c,h,'invoices',v).json()
    for amount in (1000,2000):
        assert post(c,h,'payments',dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=amount,method='CASH',paymentDate=str(business_today()))).status_code==200
    page=c.get('/api/v1/finance/ledger?limit=2',headers=h)
    assert page.status_code==200,page.text
    assert page.json()['total']==3 and len(page.json()['items'])==2
    assert c.get('/api/v1/finance/ledger?tab=due',headers=h).json()['total']==1
    assert c.get('/api/v1/finance/ledger?tab=completed',headers=h).json()['total']==2
    assert c.get('/api/v1/finance/ledger?method=CASH',headers=h).json()['total']==2
    assert c.get('/api/v1/finance/ledger?query=Rent',headers=h).json()['total']==1
    other=authorization(sign_in(c))
    assert c.get('/api/v1/finance/ledger',headers=other).json()['total']==0


def test_accountant_scope_and_invalid_payments(finance):
    c,f,h,v=finance
    invoice=post(c,h,'invoices',v).json()
    with f() as db:
        user=db.scalar(select(User).where(User.email=='owner@example.com'))
        user.role_id='ACCOUNTANT'
        db.add(PropertyAssignment(user_id=user.id,property_id='prop-sunrise'))
        db.commit()
    accountant=authorization(sign_in(c,'owner@example.com'))
    assert c.get('/api/v1/finance/options/properties',headers=accountant).json()==[{'id':'prop-sunrise','name':'Sunrise PG'}]
    assert c.get('/api/v1/finance/options/tenants?propertyId=prop-greenview',headers=accountant).json()['total']==0
    payload=dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=20,method='CASH',paymentDate=str(business_today()))
    for patch in ({'amount':0},{'amount':-1},{'status':'SUCCESS'},{'method':'CRYPTO'}, {'paymentDate':str(business_today()+timedelta(days=1))}, {'method':'UPI'}, {'tenantId':str(uuid4())}):
        assert post(c,accountant,'payments',payload|patch).status_code==422
    assert post(c,accountant,'payments',payload|{'invoiceId':str(uuid4())}).status_code==404
    first=post(c,accountant,'payments',payload|{'method':'UPI','reference':'QA-REF'})
    assert first.status_code==200,first.text
    assert post(c,accountant,'payments',payload|{'method':'UPI','reference':'qa-ref'}).status_code==409
    with f() as db:
        assert db.scalar(select(func.count()).select_from(Payment))==1


def test_overdue_history_and_summary_filters(finance):
    c,f,h,v=finance
    yesterday=str(business_today()-timedelta(days=1))
    invoice=post(c,h,'invoices',v|{'issueDate':yesterday,'dueDate':yesterday}).json()
    assert invoice['status']=='OVERDUE'
    totals=c.get('/api/v1/finance/overview?propertyId=prop-greenview',headers=h).json()
    assert totals['billed']==totals['collected']==totals['outstanding']==0
    with f() as db:
        tenant=db.get(Tenant,UUID(v['tenantId']))
        tenant.data=tenant.data|{'firstName':'Renamed'}
        db.get(Property,'prop-sunrise').name='Renamed Property'
        db.commit()
    live=c.get('/api/v1/finance/invoices/'+invoice['id'],headers=h).json()
    assert live['tenantName']=='Finance QA' and live['propertyName']=='Sunrise PG'
    assert c.get('/api/v1/finance/invoices?dueStatus=OVERDUE',headers=h).json()['total']==1
    assert c.post('/api/v1/finance/invoices/'+invoice['id']+'/cancel',headers=h).status_code==200
    payload=dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=20,method='CASH',paymentDate=str(business_today()))
    assert post(c,h,'payments',payload).status_code==409


def test_financial_reports_and_property_revenue(finance):
    c,f,h,v=finance
    invoice=post(c,h,'invoices',v).json()
    assert post(c,h,'payments',dict(tenantId=v['tenantId'],invoiceId=invoice['id'],amount=1234.50,method='CASH',paymentDate=str(business_today()))).status_code==200
    for kind in ('REVENUE','PAYMENTS','OUTSTANDING','EXPENSES'):
        response=c.get('/api/v1/finance/reports/'+kind,headers=h)
        assert response.status_code==200,response.text
        assert response.json()['type']==kind
    assert c.get('/api/v1/properties/prop-sunrise',headers=h).json()['monthlyRevenue']==1234.5
    tenant_headers=authorization(sign_in(c))
    assert c.get('/api/v1/finance/reports/REVENUE',headers=tenant_headers).status_code==403
    other=authorization(sign_in(c,'owner@example.com'))
    report=c.get('/api/v1/finance/reports/PAYMENTS',headers=other).json()
    assert report['metrics'][0]['value']=='0'
