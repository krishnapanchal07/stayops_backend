from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class BuildingWrite(Strict):
    name: str = Field(min_length=2,max_length=100)
    buildingCode: str = Field(min_length=1,max_length=100)
    description: str = Field('',max_length=1000)
    status: Literal['ACTIVE','INACTIVE','ARCHIVED'] = 'ACTIVE'

class FloorWrite(Strict):
    name: str = Field(min_length=1,max_length=100)
    floorNumber: int = Field(ge=-10,le=200)
    description: str = Field('',max_length=1000)
    status: Literal['ACTIVE','INACTIVE','ARCHIVED'] = 'ACTIVE'

class RoomWrite(Strict):
    propertyId: str = Field(min_length=1,max_length=64)
    buildingId: str = Field(min_length=1,max_length=64)
    floorId: str = Field(min_length=1,max_length=64)
    roomNumber: str = Field(min_length=1,max_length=40)
    roomType: Literal['STANDARD','PREMIUM','DELUXE']
    sharingType: Literal['1_SHARING','2_SHARING','3_SHARING','4_SHARING']
    capacity: int = Field(ge=1,le=4)
    monthlyRent: Decimal = Field(gt=0,le=10000000,max_digits=12,decimal_places=2)
    securityDeposit: Decimal = Field(ge=0,le=10000000,max_digits=12,decimal_places=2)
    status: Literal['AVAILABLE','MAINTENANCE','BLOCKED'] = 'AVAILABLE'
    amenities: list[str] = Field(default_factory=list,max_length=30)
    description: str = Field('',max_length=2000)

    @model_validator(mode='after')
    def validate_capacity(self):
        if self.capacity != int(self.sharingType[0]):
            raise ValueError('Capacity must match sharing type.')
        if any(len(a)>80 for a in self.amenities):
            raise ValueError('Amenity is too long.')
        return self

class BedWrite(Strict):
    bedNumber: str = Field(min_length=1,max_length=16)
    label: str = Field(min_length=1,max_length=80)
    rent: Decimal = Field(gt=0,le=10000000,max_digits=12,decimal_places=2)
    status: Literal['AVAILABLE','RESERVED','MAINTENANCE','BLOCKED'] = 'AVAILABLE'

class RoomStatus(Strict):
    status: Literal['AVAILABLE','MAINTENANCE','BLOCKED','ARCHIVED']

class BedStatus(Strict):
    status: Literal['AVAILABLE','RESERVED','MAINTENANCE','BLOCKED']

class HierarchyStatus(Strict):
    status: Literal['ACTIVE','INACTIVE','ARCHIVED']
