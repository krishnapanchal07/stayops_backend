from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import now
from app.modules.auth.dependencies import ensure_property_access, trusted_property_ids
from app.modules.auth.models import PropertyAssignment, User
from app.modules.finance.business_date import business_today
from app.modules.finance.service import replay
from app.modules.inventory.models import Room
from app.modules.properties.models import Property
from app.modules.staff.models import StaffProfile
from app.modules.tenants.models import Occupancy, Tenant
from app.modules.tenants.service import permission
from . import schemas
from .models import Complaint, Event, Task

MANAGERS = {'SUPER_ADMIN', 'OWNER', 'WARDEN'}
ACTIVE_TASK = ('PENDING', 'ASSIGNED', 'IN_PROGRESS')
TERMINAL = ('RESOLVED', 'CLOSED', 'REJECTED')
SLA = {'LOW': 72, 'MEDIUM': 48, 'HIGH': 24, 'URGENT': 4}
TRANSITIONS = {
    'OPEN': {'REVIEWED', 'ASSIGNED', 'REJECTED', 'ESCALATED'},
    'REVIEWED': {'ASSIGNED', 'REJECTED', 'ESCALATED'},
    'ASSIGNED': {'IN_PROGRESS', 'RESOLVED', 'ESCALATED'},
    'IN_PROGRESS': {'RESOLVED', 'ESCALATED'},
    'ESCALATED': {'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'REVIEWED'},
    'RESOLVED': {'CLOSED', 'IN_PROGRESS'},
    'REJECTED': set(),
    'CLOSED': set(),
}


def manager(user, action):
    if user.role_id not in MANAGERS:
        raise HTTPException(403, 'A complaint manager is required.')
    permission(user, f'COMPLAINT_{action}')


def scope(stmt, user, *, tasks=False):
    permission(user, 'MAINTENANCE_VIEW' if tasks and user.role_id != 'TENANT' else 'COMPLAINT_VIEW')
    if user.role_id == 'SUPER_ADMIN':
        return stmt
    if user.role_id == 'TENANT' and not tasks:
        return stmt.where(Complaint.tenant_id.in_(select(Tenant.id).where(Tenant.user_id == user.id)))
    if user.role_id in MANAGERS:
        return stmt.where(Complaint.property_id.in_(trusted_property_ids(user)))
    if user.role_id == 'MAINTENANCE' and tasks:
        return stmt.where(Task.assigned_to == user.id, Complaint.property_id.in_(trusted_property_ids(user)))
    raise HTTPException(403, 'You do not have access to this workflow.')


def get(db, key, user, *, lock=False):
    stmt = scope(select(Complaint).where(Complaint.id == key), user)
    item = db.scalar(stmt.with_for_update() if lock else stmt)
    if not item:
        raise HTTPException(404, 'Complaint not found.')
    return item


def event(db, item, user, kind, message, previous=None, *, internal=False):
    db.add(Event(complaint_id=item.id, actor_id=user.id, actor_name=user.full_name,
        actor_role=user.role_id, event_type=kind, message=message, previous_status=previous,
        new_status=item.status if previous else None, internal=internal))


def create(db, values, user, key, fingerprints=()):
    if user.role_id == 'TENANT':
        permission(user, 'COMPLAINT_CREATE')
        if values.tenantId is not None:
            raise HTTPException(422, 'Tenant identity comes from your account.')
        tenant_filter = Tenant.user_id == user.id
    else:
        manager(user, 'CREATE')
        tenant_filter = Tenant.id == values.tenantId
    existing, request_key, digest = replay(db, Complaint, user, key, schemas.Submission(
        complaint=values, attachments=list(fingerprints)))
    if existing:
        return existing, False
    row = db.execute(select(Tenant, Occupancy, Property, Room)
        .join(Occupancy, Occupancy.tenant_id == Tenant.id).join(Property, Property.id == Occupancy.property_id)
        .join(Room, Room.id == Occupancy.room_id).where(tenant_filter).with_for_update(of=Tenant)).first()
    if not row:
        raise HTTPException(409, 'An active tenant room allocation is required.')
    tenant, allocation, prop, room = row
    if user.role_id != 'TENANT':
        ensure_property_access(user, allocation.property_id)
    if values.roomId and room.id != values.roomId:
        raise HTTPException(422, 'The selected room does not match the current tenant allocation.')
    item = Complaint(tenant_id=tenant.id, property_id=prop.id, room_id=room.id, property_name=prop.name,
        room_number=room.room_number, tenant_name=f"{tenant.data['firstName']} {tenant.data['lastName']}",
        category=values.category, title=values.title, description=values.description, priority=values.priority,
        source=user.role_id if user.role_id in {'TENANT', 'WARDEN'} else 'ADMIN',
        due_at=now() + timedelta(hours=SLA[values.priority]), created_by=user.id,
        request_key=request_key, request_digest=digest)
    db.add(item)
    db.flush()
    event(db, item, user, 'CREATED', 'Complaint submitted.')
    return item, True


def eligible_staff(property_ids):
    return select(User).join(StaffProfile, StaffProfile.user_id == User.id).where(
        User.role_id == 'MAINTENANCE', User.is_active, StaffProfile.status == 'ACTIVE', StaffProfile.link_confirmed,
        User.id.in_(select(PropertyAssignment.user_id).where(PropertyAssignment.property_id.in_(property_ids))))


def active_task(db, item):
    return db.scalar(select(Task).where(Task.complaint_id == item.id, Task.status.in_(ACTIVE_TASK)).with_for_update())


def transition(db, item, user, status, kind, message):
    previous = item.status
    if status not in TRANSITIONS[previous]:
        raise HTTPException(409, 'This complaint status change is not allowed.')
    item.status = status
    item.updated_at = now()
    if status == 'RESOLVED':
        item.resolved_at = now()
    elif status == 'CLOSED':
        item.closed_at = now()
    elif previous == 'RESOLVED':
        item.resolved_at = None
    event(db, item, user, kind, message, previous)


def new_task(db, item):
    task = Task(complaint_id=item.id, assigned_to=item.assigned_to, assigned_name=item.assigned_name,
        priority=item.priority, due_at=item.due_at, estimated_cost=item.estimated_cost, scheduled_date=business_today())
    db.add(task)
    return task


def assign(db, item, user, values):
    staff = db.scalar(eligible_staff([item.property_id]).where(User.id == values.staffId))
    if not staff:
        raise HTTPException(422, 'Select an active maintenance employee assigned to this property.')
    if item.status not in {'OPEN', 'REVIEWED', 'ESCALATED', 'ASSIGNED'}:
        raise HTTPException(409, 'This complaint cannot be assigned in its current status.')
    task = active_task(db, item)
    if (task and item.status == 'ASSIGNED' and task.assigned_to == staff.id
            and values.priority in (None, item.priority) and values.dueAt in (None, item.due_at)
            and ('estimatedCost' not in values.model_fields_set or values.estimatedCost == item.estimated_cost)):
        return task
    if task and task.assigned_to != staff.id:
        task.status = 'CANCELLED'
        db.flush()
        task = None
    if values.dueAt and values.dueAt != item.due_at and values.dueAt <= now():
        raise HTTPException(422, 'Select a future due time.')
    if values.priority and values.priority != item.priority:
        item.priority = values.priority
        item.due_at = item.created_at + timedelta(hours=SLA[item.priority])
    if values.dueAt:
        item.due_at = values.dueAt
    if 'estimatedCost' in values.model_fields_set:
        item.estimated_cost = values.estimatedCost
    old_status = item.status
    reassigned = item.assigned_to is not None and item.assigned_to != staff.id
    item.assigned_to, item.assigned_name = staff.id, staff.full_name
    item.status = 'IN_PROGRESS' if task and task.status == 'IN_PROGRESS' else 'ASSIGNED'
    if task:
        task.priority, task.due_at = item.priority, item.due_at
        task.estimated_cost = item.estimated_cost
    else:
        task = new_task(db, item)
    event(db, item, user, 'REASSIGNED' if reassigned else 'ASSIGNED', 'Maintenance assignment saved.', old_status)
    db.flush()
    return task


def resolve(db, item, user, values, task=None, completed_at=None):
    transition(db, item, user, 'RESOLVED', 'RESOLVED', 'Complaint resolved.')
    item.resolution_notes = values.resolutionNotes
    item.actual_cost = values.actualCost
    linked = task or active_task(db, item)
    if linked:
        linked.status = 'COMPLETED'
        linked.completed_at = completed_at or now()
        linked.parts_used = values.partsUsed if task else linked.parts_used
        linked.resolution_notes = values.resolutionNotes
        linked.actual_cost = values.actualCost
