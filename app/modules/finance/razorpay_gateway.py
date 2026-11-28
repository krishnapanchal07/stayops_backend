"""The only boundary that communicates with Razorpay."""
from typing import Literal

import razorpay
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from razorpay.errors import BadRequestError, GatewayError, ServerError, SignatureVerificationError
from requests import RequestException

from app.core.config import get_settings


class GatewayUnavailable(Exception):
    pass


class ProviderOrder(BaseModel):
    id: str = Field(pattern=r'^order_[A-Za-z0-9]{1,55}$')
    receipt: str = Field(max_length=40)
    amount: int = Field(strict=True, gt=0)
    currency: str


class ProviderPayment(BaseModel):
    id: str = Field(pattern=r'^pay_[A-Za-z0-9]{1,59}$')
    order_id: str = Field(pattern=r'^order_[A-Za-z0-9]{1,55}$')
    amount: int = Field(strict=True, gt=0)
    currency: str
    status: Literal['created', 'authorized', 'captured', 'refunded', 'failed']
    captured: bool = Field(strict=True)
    amount_refunded: int = Field(default=0, strict=True, ge=0)
    method: str = Field(max_length=40)
    created_at: int = Field(strict=True, ge=0, le=253402214399)


class RazorpayGateway:
    def __init__(self):
        settings = get_settings()
        if not settings.razorpay_ready:
            raise HTTPException(503, 'Razorpay Test Mode is unavailable.')
        self.client = razorpay.Client(auth=(settings.razorpay_key_id.get_secret_value(),
            settings.razorpay_key_secret.get_secret_value()))

    @staticmethod
    def call(operation, *args):
        try:
            return operation(*args, timeout=(3, 8), allow_redirects=False)
        except (RequestException, BadRequestError, GatewayError, ServerError, ValueError):
            # SDK error text can include provider data. Keep it out of logs and responses.
            raise GatewayUnavailable from None

    @staticmethod
    def parse(model, data):
        try:
            return model.model_validate(data)
        except ValidationError:
            raise GatewayUnavailable from None

    def create_order(self, receipt, amount):
        data = self.call(self.client.order.create, dict(amount=amount, currency='INR', receipt=receipt, partial_payment=False))
        return self.parse(ProviderOrder, data)

    def find_order(self, receipt):
        data = self.call(self.client.order.all, {'receipt': receipt, 'count': 2})
        matches = [self.parse(ProviderOrder, row) for row in self.items(data) if row.get('receipt') == receipt]
        if len(matches) > 1:
            raise GatewayUnavailable
        return matches[0] if matches else None

    def fetch_order(self, order_id):
        return self.parse(ProviderOrder, self.call(self.client.order.fetch, order_id))

    def verify_checkout(self, order_id, payment_id, signature):
        try:
            self.client.utility.verify_payment_signature(dict(razorpay_order_id=order_id,
                razorpay_payment_id=payment_id, razorpay_signature=signature))
        except SignatureVerificationError:
            raise HTTPException(400, 'Invalid Razorpay checkout signature.') from None

    def fetch_payment(self, payment_id):
        return self.parse(ProviderPayment, self.call(self.client.payment.fetch, payment_id))

    @staticmethod
    def items(data):
        if not isinstance(data, dict) or not isinstance(data.get('items'), list) or any(not isinstance(x, dict) for x in data['items']):
            raise GatewayUnavailable
        return data['items']

    def order_payments(self, order_id):
        data = self.call(self.client.order.payments, order_id)
        return [self.parse(ProviderPayment, row) for row in self.items(data)]
