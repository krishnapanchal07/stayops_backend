from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.modules.finance.business_date import business_today

Money = Annotated[Decimal, Field(ge=0, le=9999999999, decimal_places=2, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, le=9999999999, decimal_places=2, allow_inf_nan=False)]
Method = Literal['CASH','UPI','BANK_TRANSFER','CARD','OTHER']

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class Item(Input):
    id: str = Field('', max_length=80)
    type: Literal['RENT','ELECTRICITY','MAINTENANCE','LATE_FEE','OTHER']
    description: str = Field(min_length=1, max_length=500)
    amount: PositiveMoney

class InvoiceWrite(Input):
    tenantId: UUID
    propertyId: str | None = Field(None, max_length=64)
    billingPeriod: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    issueDate: date
    dueDate: date
    items: list[Item] = Field(min_length=1, max_length=50)
    discount: Money = Decimal(0)
    lateFee: Money = Decimal(0)

    @model_validator(mode='after')
    def valid_dates(self):
        if self.dueDate < self.issueDate:
            raise ValueError('Due date cannot precede issue date.')
        return self

class PaymentWrite(Input):
    tenantId: UUID
    invoiceId: UUID
    amount: PositiveMoney
    method: Method
    paymentDate: date
    reference: str = Field('', max_length=160)
    notes: str = Field('', max_length=2000)

    @model_validator(mode='after')
    def valid_payment(self):
        if self.paymentDate > business_today():
            raise ValueError('Payment date cannot be in the future.')
        if self.method in {'UPI','BANK_TRANSFER','CARD'} and not self.reference:
            raise ValueError('Transaction reference is required for this method.')
        return self

class ExpenseWrite(Input):
    propertyId: str = Field(min_length=1, max_length=64)
    category: Literal['ELECTRICITY','WATER','INTERNET','MAINTENANCE','CLEANING','SALARY','SUPPLIES','REPAIRS','OTHER']
    description: str = Field(min_length=1, max_length=1000)
    amount: PositiveMoney
    expenseDate: date
    vendor: str = Field('', max_length=200)
    paymentMethod: Method
    status: Literal['PAID','PENDING','CANCELLED']
    notes: str = Field('', max_length=2000)

class Refund(Input):
    reason: str = Field(min_length=1, max_length=2000)
