from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from app.modules.finance.schemas import Input

Category = Literal['PLUMBING','ELECTRICAL','INTERNET','CLEANING','FURNITURE','AC','WATER','SECURITY','ROOM','NOISE','HOUSEKEEPING','OTHER']
Priority = Literal['LOW','MEDIUM','HIGH','URGENT']
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]


class Create(Input):
    tenantId: UUID | None = None
    roomId: str = Field('', max_length=64)
    category: Category
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=12, max_length=5000)
    priority: Priority


class Submission(Input):
    complaint: Create
    attachments: list[tuple[str, str, str]]


class Assignment(Input):
    staffId: UUID
    dueAt: AwareDatetime | None = None
    priority: Priority | None = None
    estimatedCost: Money | None = None


class Transition(Input):
    status: Literal['REVIEWED','IN_PROGRESS','REJECTED','CANCELLED']


class Escalation(Input):
    reason: str = Field(min_length=1, max_length=2000)


class Resolution(Input):
    resolutionNotes: str = Field(min_length=1, max_length=4000)
    actualCost: Money | None = None


class Completion(Resolution):
    actualCost: Money
    partsUsed: str = Field('', max_length=2000)
    completedAt: date


class CommentWrite(Input):
    message: str = Field(min_length=1, max_length=3000)
    visibility: Literal['PUBLIC','INTERNAL'] = 'PUBLIC'
