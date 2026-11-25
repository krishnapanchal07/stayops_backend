from datetime import date
from typing import Literal

from pydantic import EmailStr, Field, TypeAdapter, create_model, field_validator, model_validator

from app.modules.auth.schemas import Input, ProfileInput
from app.modules.inquiries.schemas import Contact

TenantStatus = Literal['PROSPECT', 'ONBOARDING', 'ACTIVE', 'NOTICE_PERIOD', 'CHECKED_OUT', 'INACTIVE']


class PersonalInput(Input):
    phone: str = Field(pattern=r'^\d{10}$')
    profileImage: str = Field('', max_length=7_000_000)
    addressLine1: str = Field('', max_length=250)
    addressLine2: str = Field('', max_length=250)
    city: str = Field('', max_length=100)
    state: str = Field('', max_length=100)
    postalCode: str = Field('', max_length=20)
    country: str = Field('India', max_length=100)
    emergencyName: str = Field('', max_length=120)
    emergencyRelationship: str = Field('', max_length=80)
    emergencyPhone: str = Field('', pattern=r'^(\d{10})?$')
    emergencyEmail: str = Field('', max_length=320)
    emergencyAddress: str = Field('', max_length=500)
    @field_validator('profileImage')
    @classmethod
    def image(cls, value):
        return ProfileInput.validate_avatar(value) or ''

    @field_validator('emergencyEmail')
    @classmethod
    def emergency_email(cls, value):
        return str(TypeAdapter(EmailStr).validate_python(value.strip().lower())) if value.strip() else ''


class TenantWrite(Contact, PersonalInput):
    dateOfBirth: date | None = None
    gender: Literal['', 'MALE', 'FEMALE', 'OTHER', 'PREFER_NOT_TO_SAY'] = ''
    propertyId: str | None = Field(None, max_length=64)
    buildingId: str | None = Field(None, max_length=64)
    floorId: str | None = Field(None, max_length=64)
    roomId: str | None = Field(None, max_length=64)
    bedId: str | None = Field(None, max_length=64)
    moveInDate: date | None = None
    expectedMoveOutDate: date | None = None
    roomRent: float = Field(0, ge=0, le=10000000, allow_inf_nan=False)
    securityDeposit: float = Field(0, ge=0, le=10000000, allow_inf_nan=False)
    status: TenantStatus = 'ONBOARDING'
    source: Literal['DIRECT', 'WALK_IN', 'INQUIRY'] = 'DIRECT'
    internalNotes: str = Field('', max_length=5000)

    @model_validator(mode='after')
    def dates(self):
        if self.moveInDate and self.expectedMoveOutDate and self.expectedMoveOutDate < self.moveInDate:
            raise ValueError('Move-out must follow move-in')
        if self.dateOfBirth and self.dateOfBirth > date.today():
            raise ValueError('Date of birth cannot be in the future')
        return self


# PATCH validates provided fields; the merged record is revalidated with TenantWrite in service.
TenantPatch = create_model('TenantPatch', __base__=Input, **{
    name: (field.annotation | None, None) for name, field in TenantWrite.model_fields.items()
})


class StatusInput(Input):
    status: TenantStatus


class AllocateInput(Input):
    roomId: str = Field(min_length=1, max_length=64)
    bedId: str = Field(min_length=1, max_length=64)
    moveInDate: date


class CheckoutInput(Input):
    moveOutDate: date = Field(default_factory=date.today)


class AcceptInvitation(Input):
    token: str = Field(min_length=20, max_length=256)
