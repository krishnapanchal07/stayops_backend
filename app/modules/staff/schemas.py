from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, create_model, field_validator, model_validator

from app.modules.auth.schemas import EmailInput, Input, ProfileInput

StaffRole = Literal['WARDEN', 'ACCOUNTANT', 'SECURITY', 'MAINTENANCE']
StaffStatus = Literal['ACTIVE', 'INACTIVE', 'ON_LEAVE']
Department = Literal['OPERATIONS', 'FINANCE', 'SECURITY', 'MAINTENANCE']


class StaffFields(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, hide_input_in_errors=True)
    firstName: str = Field(min_length=1, max_length=120)
    lastName: str = Field(max_length=119)
    phone: str = Field(pattern=r'^\d{10}$')
    department: Department
    designation: str = Field(min_length=1, max_length=120)
    joiningDate: date
    profileImage: str = Field('', max_length=7_000_000)
    addressLine1: str = Field(min_length=1, max_length=250)
    addressLine2: str = Field('', max_length=250)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postalCode: str = Field('', max_length=20)
    country: str = Field('India', min_length=1, max_length=100)
    emergencyName: str = Field(min_length=1, max_length=120)
    emergencyRelationship: str = Field('', max_length=80)
    emergencyPhone: str = Field(pattern=r'^\d{10}$')

    @field_validator('profileImage')
    @classmethod
    def image(cls, value):
        return ProfileInput.validate_avatar(value) or ''

    @model_validator(mode='after')
    def full_name_length(self):
        if len(f'{self.firstName} {self.lastName}'.strip()) > 120:
            raise ValueError('Name must be at most 120 characters')
        return self


class Assignments(Input):
    propertyIds: list[str] = Field(min_length=1, max_length=50)

    @field_validator('propertyIds')
    @classmethod
    def references(cls, values):
        if any(not value or len(value) > 64 for value in values) or len(set(values)) != len(values):
            raise ValueError('Use distinct valid property references')
        return values


class StaffCreate(StaffFields, EmailInput, Assignments):
    role: StaffRole
    status: StaffStatus = 'ACTIVE'
    # Explicit administrator intent, followed by authenticated mailbox confirmation.
    existingUserId: UUID | None = None

    @model_validator(mode='after')
    def new_account_name(self):
        if not self.existingUserId and not self.lastName:
            raise ValueError('Last name is required for a new staff account')
        return self


# Identity email, role, status, assignments and account flags have dedicated boundaries.
StaffPatch = create_model('StaffPatch', __base__=Input, **{
    name: (field.annotation | None, None) for name, field in StaffFields.model_fields.items()
}, propertyIds=(list[str] | None, None), status=(StaffStatus | None, None))


class StatusInput(Input):
    status: StaffStatus


class AcceptInvitation(Input):
    token: str = Field(min_length=20, max_length=256)
