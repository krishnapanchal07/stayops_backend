from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, EmailStr, TypeAdapter, field_validator, model_validator


class PropertyWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=80)
    type: Literal['PG', 'HOSTEL', 'CO_LIVING']
    address: str = Field(min_length=1, max_length=160)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    locality: str = Field('', max_length=100)
    country: str = Field('India', min_length=1, max_length=80)
    postalCode: str = Field('', pattern=r'^([0-9]{6})?$')
    contactNumber: str = Field('', pattern=r'^([6-9][0-9]{9})?$')
    email: str = Field('', max_length=320)
    description: str = Field('', max_length=500)
    amenities: list[str] = Field(default_factory=list, max_length=30)
    status: Literal['ACTIVE', 'INACTIVE', 'MAINTENANCE', 'ARCHIVED'] = 'ACTIVE'
    ownerUserId: UUID | None = None
    latitude: float | None = Field(None, ge=-85.05112878, le=85.05112878, allow_inf_nan=False)
    longitude: float | None = Field(None, ge=-180, le=180, allow_inf_nan=False)
    isPublic: bool = False
    isDemo: bool = False

    @field_validator('email')
    @classmethod
    def email_valid(cls, value):
        return str(TypeAdapter(EmailStr).validate_python(value)) if value else ''

    @field_validator('amenities')
    @classmethod
    def amenities_valid(cls, values):
        if any(not value.strip() or len(value) > 60 for value in values):
            raise ValueError('Amenities must contain 1–60 characters.')
        return sorted(set(values))

    @model_validator(mode='after')
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Provide both latitude and longitude, or neither.')
        return self


class CoverInput(BaseModel):
    imageId: UUID


class OrderInput(BaseModel):
    imageIds: list[UUID] = Field(max_length=12)


class StatusInput(BaseModel):
    status: Literal['ACTIVE', 'INACTIVE', 'MAINTENANCE']
