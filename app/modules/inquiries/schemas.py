from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from app.modules.auth.schemas import EmailInput, Input

InquiryStatus = Literal['SUBMITTED', 'CONTACTED', 'SHORTLISTED', 'APPROVED', 'CONVERTED', 'REJECTED', 'WITHDRAWN']


class Contact(EmailInput):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, hide_input_in_errors=True)
    firstName: str = Field(min_length=1, max_length=60)
    lastName: str = Field(min_length=1, max_length=60)
    phone: str = Field(pattern=r'^\d{10}$')


class RoomInquiry(Contact):
    propertyId: str | None = Field(None, max_length=64)
    roomId: str = Field(min_length=1, max_length=64)
    bedId: str | None = Field(None, min_length=1, max_length=64)
    preferredMoveIn: date | None = None
    message: str = Field(min_length=8, max_length=2000)


class GeneralInquiry(EmailInput):
    name: str = Field(min_length=1, max_length=120)
    inquiryType: Literal['tenant', 'owner', 'staff']

    @field_validator('name')
    @classmethod
    def name_required(cls, value):
        if not value.strip():
            raise ValueError('Name is required')
        return value.strip()


class StatusInput(Input):
    status: InquiryStatus
    note: str | None = Field(None, max_length=2000)


class ConvertInput(Input):
    allocateBed: bool = False
