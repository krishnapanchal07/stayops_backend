from datetime import date, datetime, time
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.security import now
from app.db.session import get_db
from app.modules.auth.dependencies import ensure_property_access, get_active_user, require_trusted_origin, trusted_property_ids
from app.modules.auth.models import User
from app.modules.finance.business_date import BUSINESS_TIMEZONE, business_today
from app.modules.inventory.models import Floor, Room
from app.modules.properties.images import MAX_IMAGE_SIZE_BYTES, process
from app.modules.properties.models import Property
from app.modules.properties.storage import storage
from app.modules.tenants.models import Occupancy, Tenant
from app.modules.tenants.service import permission
from . import schemas, service as s
from .models import Attachment, Comment, Complaint, Event, Task

router = APIRouter(tags=['Complaints and maintenance'])
write = [Depends(require_trusted_origin)]


class Filters(BaseModel):
    query: str = Field('', max_length=120)
    propertyId: str = Field('', max_length=64)
    buildingId: str = Field('', max_length=64)
    floorId: str = Field('', max_length=64)
    roomId: str = Field('', max_length=64)
    tenantId: UUID | None = None
    complaintId: UUID | None = None
    assignedTo: UUID | None = None
    category: str = Field('', max_length=24)
    priority: str = Field('', max_length=12)
    status: str = Field('', max_length=20)
    tab: str = Field('', max_length=20)
    overdueOnly: bool = False
    fromDate: date | None = None
    toDate: date | None = None
    sortBy: str = Field('createdAt', max_length=32)
    sortDirection: str = Field('desc', pattern='^(asc|desc)$')
    offset: int = Field(0, ge=0)
    limit: int = Field(20, ge=1, le=100)


def attachment_output(item):
    return dict(id=item.id, complaintId=item.complaint_id, fileName=item.file_name, fileType='image/webp',
        fileSize=item.file_size, previewUrl=f'/complaints/{item.complaint_id}/attachments/{item.id}',
        uploadedBy=item.actor_id, uploadedAt=item.created_at)


def complaint_output(item, user, attachments=()):
    tenant = user.role_id == 'TENANT'
    return dict(id=item.id, complaintCode=item.code, title=item.title, description=item.description,
        tenantId=item.tenant_id, tenantName=item.tenant_name, propertyId=item.property_id, propertyName=item.property_name,
        roomId=item.room_id, roomNumber=item.room_number, category=item.category, status=item.status,
        priority=item.priority, assignedTo=None if tenant else item.assigned_to, assignedToName=None if tenant else item.assigned_name,
        createdAt=item.created_at, updatedAt=item.updated_at, dueAt=item.due_at, resolvedAt=item.resolved_at, closedAt=item.closed_at,
        resolutionNotes=item.resolution_notes, estimatedCost=None if tenant else item.estimated_cost,
        actualCost=None if tenant else item.actual_cost, escalationLevel=item.escalation_level,
        source=item.source, attachments=[attachment_output(a) for a in attachments])


def details(db, item, user):
    attachments = db.scalars(select(Attachment).where(Attachment.complaint_id == item.id).order_by(Attachment.created_at))
    return complaint_output(item, user, attachments)


def task_output(task, item):
    return dict(id=task.id, taskCode=task.code, complaintId=item.id, complaintCode=item.code, title=item.title,
        description=item.description, propertyId=item.property_id, propertyName=item.property_name,
        roomId=item.room_id, roomNumber=item.room_number, priority=task.priority,
        assignedTo=task.assigned_to, assignedToName=task.assigned_name, status=task.status,
        scheduledDate=task.scheduled_date, dueDate=task.due_at, estimatedCost=task.estimated_cost,
        actualCost=task.actual_cost, partsUsed=task.parts_used, resolutionNotes=task.resolution_notes,
        createdAt=task.created_at, updatedAt=task.updated_at, completedAt=task.completed_at)


def listing(db, user, f, tasks=False):
    model = Task if tasks else Complaint
    stmt = select(Task, Complaint).join(Complaint, Complaint.id == Task.complaint_id) if tasks else select(Complaint)
    stmt = s.scope(stmt, user, tasks=tasks)
    for column, value in [(Complaint.property_id, f.propertyId), (Complaint.tenant_id, f.tenantId),
            (Complaint.room_id, f.roomId), (Complaint.category, f.category), (model.priority, f.priority),
            (model.status, f.status), (model.assigned_to, f.assignedTo), (Complaint.id, f.complaintId)]:
        if value:
            stmt = stmt.where(column == value)
    if f.floorId or f.buildingId:
        stmt = stmt.join(Room, Room.id == Complaint.room_id).join(Floor, Floor.id == Room.floor_id)
        if f.floorId:
            stmt = stmt.where(Floor.id == f.floorId)
        if f.buildingId:
            stmt = stmt.where(Floor.building_id == f.buildingId)
    if f.query.strip():
        stmt = stmt.where(or_(*(column.icontains(f.query.strip(), autoescape=True)
            for column in (model.code, Complaint.title, Complaint.tenant_name, Complaint.room_number, Complaint.code))))
    if f.fromDate:
        stmt = stmt.where(model.created_at >= datetime.combine(f.fromDate, time.min, BUSINESS_TIMEZONE))
    if f.toDate:
        stmt = stmt.where(model.created_at <= datetime.combine(f.toDate, time.max, BUSINESS_TIMEZONE))
    if f.overdueOnly or f.tab == 'OVERDUE':
        stmt = stmt.where(Complaint.status.not_in(s.TERMINAL), Complaint.due_at < now())
    groups = {'OPEN': ('OPEN','REVIEWED'), 'IN_PROGRESS': ('ASSIGNED','IN_PROGRESS','ESCALATED'),
              'RESOLVED': ('RESOLVED','CLOSED')}
    if not tasks and f.tab in groups:
        stmt = stmt.where(Complaint.status.in_(groups[f.tab]))
    count = db.scalar(select(func.count()).select_from(stmt.subquery()))
    columns = {'createdAt': model.created_at, 'created': model.created_at, 'updatedAt': model.updated_at,
        'updated': model.updated_at, 'status': model.status, 'priority': model.priority, 'dueAt': model.due_at,
        'dueDate':model.due_at, 'code': model.code, 'title': Complaint.title, 'tenant': Complaint.tenant_name,
        'property': Complaint.property_name, 'room': Complaint.room_number, 'issue': Complaint.title,
        'complaint': Complaint.code, 'assignee': model.assigned_name, 'category': Complaint.category}
    sort = columns.get(f.sortBy, model.created_at)
    rows = db.execute(stmt.order_by(sort.asc() if f.sortDirection == 'asc' else sort.desc(), model.id)
        .offset(f.offset).limit(f.limit))
    items = [task_output(task, item) for task, item in rows] if tasks else [complaint_output(row[0], user) for row in rows]
    return dict(items=items, total=count, offset=f.offset, limit=f.limit)


@router.get('/complaints')
def complaints(f: Filters = Depends(), db: Session = Depends(get_db), user=Depends(get_active_user)):
    return listing(db, user, f)


@router.get('/complaints/summary')
def complaint_summary(propertyId: str = '', tenantId: UUID | None = None,
                      db: Session = Depends(get_db), user=Depends(get_active_user)):
    q = s.scope(select(Complaint), user)
    if propertyId:
        q = q.where(Complaint.property_id == propertyId)
    if tenantId:
        q = q.where(Complaint.tenant_id == tenantId)
    q = q.subquery()
    active = q.c.status.not_in(s.TERMINAL)
    month = datetime.combine(business_today().replace(day=1), time.min, BUSINESS_TIMEZONE)
    return db.execute(select(func.count().label('total'),
        func.count().filter(q.c.status.in_(('OPEN','REVIEWED'))).label('open'),
        func.count().filter(q.c.status.in_(('ASSIGNED','IN_PROGRESS','ESCALATED'))).label('inProgress'),
        func.count().filter(q.c.status.in_(('RESOLVED','CLOSED'))).label('resolved'),
        func.count().filter(active, q.c.due_at < now()).label('overdue'),
        func.count().filter(active, q.c.priority == 'URGENT').label('urgent'),
        func.count().filter(q.c.resolved_at >= month).label('resolvedThisMonth'),
        func.max(q.c.created_at).label('lastComplaint')).select_from(q)).mappings().one()


@router.get('/complaints/options/staff')
def staff_options(propertyId: str = Query('', max_length=64), limit: int = Query(100, ge=1, le=100),
                  offset: int = Query(0, ge=0), db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'ASSIGN')
    if propertyId:
        ensure_property_access(user, propertyId)
    properties = [propertyId] if propertyId else (
        select(Property.id) if user.role_id == 'SUPER_ADMIN' else trusted_property_ids(user))
    stmt = s.eligible_staff(properties)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    return dict(total=total, offset=offset, limit=limit, items=[dict(id=staff.id, name=staff.full_name, roleId=staff.role_id)
        for staff in db.scalars(stmt.order_by(User.full_name, User.id).limit(limit).offset(offset))])


@router.get('/complaints/options/tenants')
def tenant_options(filters: Filters = Depends(), db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'CREATE')
    stmt = select(Tenant, Property.name, Room.room_number).join(Occupancy, Occupancy.tenant_id == Tenant.id
        ).join(Property, Property.id == Occupancy.property_id).join(Room, Room.id == Occupancy.room_id)
    if user.role_id != 'SUPER_ADMIN':
        stmt = stmt.where(Occupancy.property_id.in_(trusted_property_ids(user)))
    if filters.propertyId:
        stmt = stmt.where(Occupancy.property_id == filters.propertyId)
    if filters.roomId:
        stmt = stmt.where(Occupancy.room_id == filters.roomId)
    if filters.buildingId or filters.floorId:
        stmt = stmt.join(Floor, Floor.id == Room.floor_id)
        if filters.buildingId:
            stmt = stmt.where(Floor.building_id == filters.buildingId)
        if filters.floorId:
            stmt = stmt.where(Floor.id == filters.floorId)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = [dict(id=t.id, name=f"{t.data['firstName']} {t.data['lastName']}", propertyName=prop,
        roomId=t.room_id, roomNumber=number) for t, prop, number in db.execute(
        stmt.order_by(Tenant.tenant_code).offset(filters.offset).limit(filters.limit))]
    return dict(items=items, total=total, offset=filters.offset, limit=filters.limit)


@router.post('/complaints', dependencies=write, status_code=201)
def create_complaint(payload: str = Form(), files: list[UploadFile] = File(default=[]),
                     key: str | None = Header(None, alias='Idempotency-Key'),
                     db: Session = Depends(get_db), user=Depends(get_active_user)):
    if len(files) > 4:
        raise HTTPException(422, 'Attach at most four images.')
    try:
        values = schemas.Create.model_validate_json(payload)
    except ValidationError:
        raise HTTPException(422, 'Check the complaint form fields.') from None
    uploads = [(file.filename or '', file.content_type or '', file.file.read(MAX_IMAGE_SIZE_BYTES)) for file in files]
    fingerprints = [(name, content_type, sha256(data).hexdigest()) for name, content_type, data in uploads]
    item, created = s.create(db, values, user, key, fingerprints)
    stored = []
    try:
        if created:
            for name, content_type, data in uploads:
                filename, _, _, variants = process(data, name, content_type)
                content = variants['large'][0]
                attachment = Attachment(id=uuid4(), complaint_id=item.id, actor_id=user.id, file_name=filename, file_size=len(content))
                key = f'{attachment.id.hex}/large.webp'
                storage.put(key, content)
                stored.append(key)
                db.add(attachment)
        db.commit()
    except Exception:
        db.rollback()
        for key in stored:
            storage.delete(key)
        raise
    return details(db, item, user)


@router.get('/complaints/{key}')
def read_complaint(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    return details(db, s.get(db, key, user), user)


@router.get('/complaints/{key}/attachments/{attachment_id}')
def attachment(key: UUID, attachment_id: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.get(db, key, user)
    file = db.scalar(select(Attachment).where(Attachment.id == attachment_id, Attachment.complaint_id == key))
    if not file:
        raise HTTPException(404, 'Attachment not found.')
    path = storage.path(f'{file.id.hex}/large.webp')
    if not path.is_file():
        raise HTTPException(404, 'Attachment file not found.')
    return FileResponse(path, media_type='image/webp', filename=PurePosixPath(file.file_name).stem + '.webp')


@router.post('/complaints/{key}/assign', dependencies=write)
def assign(key: UUID, values: schemas.Assignment, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'ASSIGN')
    item = s.get(db, key, user, lock=True)
    task = s.assign(db, item, user, values)
    db.commit()
    return dict(complaint=details(db, item, user), task=task_output(task, item))


@router.patch('/complaints/{key}/status', dependencies=write)
def status(key: UUID, values: schemas.Transition, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'UPDATE')
    item = s.get(db, key, user, lock=True)
    task = s.active_task(db, item)
    if values.status == 'IN_PROGRESS':
        if not task or task.status != 'ASSIGNED':
            raise HTTPException(409, 'Assign a maintenance task before starting work.')
        task.status = 'IN_PROGRESS'
    s.transition(db, item, user, values.status, values.status, f'Complaint moved to {values.status.lower().replace("_", " ")}.')
    if values.status in ('REJECTED', 'REVIEWED') and task:
        task.status = 'CANCELLED'
        item.assigned_to = item.assigned_name = None
    db.commit()
    return details(db, item, user)


@router.post('/complaints/{key}/escalate', dependencies=write)
def escalate(key: UUID, values: schemas.Escalation, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'UPDATE')
    item = s.get(db, key, user, lock=True)
    s.transition(db, item, user, 'ESCALATED', 'ESCALATED', 'Complaint escalated.')
    item.escalation_level += 1
    s.event(db, item, user, 'ESCALATION_REASON', values.reason, internal=True)
    db.commit()
    return details(db, item, user)


@router.post('/complaints/{key}/resolve', dependencies=write)
def resolve(key: UUID, values: schemas.Resolution, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'RESOLVE')
    item = s.get(db, key, user, lock=True)
    s.resolve(db, item, user, values)
    db.commit()
    return details(db, item, user)


def confirm(key, action, db, user):
    if user.role_id != 'TENANT':
        s.manager(user, 'RESOLVE')
    item = s.get(db, key, user, lock=True)
    if item.status != 'RESOLVED':
        raise HTTPException(409, 'The complaint must be resolved first.')
    if action == 'close':
        s.transition(db, item, user, 'CLOSED', 'CLOSED', 'Resolution confirmed.')
    else:
        s.transition(db, item, user, 'IN_PROGRESS', 'REOPENED', 'Complaint reopened.')
        item.actual_cost, item.resolution_notes = None, ''
        staff = db.scalar(s.eligible_staff([item.property_id]).where(User.id == item.assigned_to))
        if staff:
            s.new_task(db, item)
        else:
            item.status, item.assigned_to, item.assigned_name = 'ESCALATED', None, None
            s.event(db, item, user, 'ESCALATED', 'Reopened complaint requires a new maintenance assignment.', 'IN_PROGRESS')
    db.commit()
    return details(db, item, user)


@router.post('/complaints/{key}/close', dependencies=write)
def close(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    return confirm(key, 'close', db, user)


@router.post('/complaints/{key}/reopen', dependencies=write)
def reopen(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    return confirm(key, 'reopen', db, user)


def comment_output(row, user):
    redact = user.role_id == 'TENANT' and row.actor_id != user.id
    return dict(id=row.id, complaintId=row.complaint_id, authorId='' if redact else row.actor_id,
        authorName='Property team' if redact else row.actor_name,
        authorRole=row.actor_role, message=row.message, visibility=row.visibility, createdAt=row.created_at)


@router.get('/complaints/{key}/comments')
def comments(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.get(db, key, user)
    stmt = select(Comment).where(Comment.complaint_id == key)
    if user.role_id == 'TENANT':
        stmt = stmt.where(Comment.visibility == 'PUBLIC')
    return [comment_output(row, user) for row in db.scalars(stmt.order_by(Comment.created_at, Comment.id))]


@router.post('/complaints/{key}/comments', dependencies=write)
def comment(key: UUID, values: schemas.CommentWrite, db: Session = Depends(get_db), user=Depends(get_active_user)):
    if user.role_id != 'TENANT':
        s.manager(user, 'UPDATE')
    elif values.visibility != 'PUBLIC':
        raise HTTPException(403, 'Tenants can only publish public comments.')
    item = s.get(db, key, user, lock=True)
    if item.status in ('CLOSED','REJECTED'):
        raise HTTPException(409, 'This complaint no longer accepts comments.')
    row = Comment(complaint_id=item.id, actor_id=user.id, actor_name=user.full_name, actor_role=user.role_id,
        message=values.message, visibility=values.visibility)
    db.add(row)
    db.commit()
    return comment_output(row, user)


@router.get('/complaints/{key}/timeline')
def timeline(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.get(db, key, user)
    stmt = select(Event).where(Event.complaint_id == key)
    tenant = user.role_id == 'TENANT'
    if tenant:
        stmt = stmt.where(Event.internal.is_(False))
    return [dict(id=row.id, complaintId=key, type=row.event_type, message=row.message,
        actorId='' if tenant else row.actor_id, actorName=('You' if row.actor_id == user.id else 'Property team') if tenant else row.actor_name,
        actorRole=row.actor_role, createdAt=row.created_at)
        for row in db.scalars(stmt.order_by(Event.created_at, Event.id))]


def task_record(db, key, user, lock=False):
    stmt = s.scope(select(Task, Complaint).join(Complaint, Complaint.id == Task.complaint_id).where(Task.id == key), user, tasks=True)
    row = db.execute(stmt).first()
    if not row:
        raise HTTPException(404, 'Maintenance task not found.')
    if lock:
        # Operations on both records always lock the complaint first.
        item = db.get(Complaint, row[1].id, with_for_update=True, populate_existing=True)
        task = db.get(Task, key, with_for_update=True, populate_existing=True)
        if user.role_id == 'MAINTENANCE' and task.assigned_to != user.id:
            raise HTTPException(404, 'Maintenance task not found.')
        return task, item
    return row


@router.get('/maintenance/tasks')
def tasks(f: Filters = Depends(), db: Session = Depends(get_db), user=Depends(get_active_user)):
    return listing(db, user, f, tasks=True)


@router.get('/maintenance/tasks/summary')
def task_summary(db: Session = Depends(get_db), user=Depends(get_active_user)):
    q = s.scope(select(Task).join(Complaint, Complaint.id == Task.complaint_id), user, tasks=True).subquery()
    active = q.c.status.in_(s.ACTIVE_TASK)
    return db.execute(select(func.count().filter(active, q.c.assigned_to.is_not(None)).label('assigned'),
        func.count().filter(q.c.status.in_(('PENDING','ASSIGNED'))).label('new'),
        func.count().filter(q.c.status == 'IN_PROGRESS').label('inProgress'),
        func.count().filter(active, q.c.priority == 'URGENT').label('urgent'),
        func.count().filter(q.c.status == 'COMPLETED').label('completed')).select_from(q)).mappings().one()


@router.get('/maintenance/tasks/{key}')
def read_task(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    return task_output(*task_record(db, key, user))


@router.post('/maintenance/tasks/{key}/start', dependencies=write)
def start(key: UUID, db: Session = Depends(get_db), user=Depends(get_active_user)):
    permission(user, 'MAINTENANCE_UPDATE')
    task, item = task_record(db, key, user, lock=True)
    if task.status != 'ASSIGNED' or not db.scalar(s.eligible_staff([item.property_id]).where(User.id == task.assigned_to)):
        raise HTTPException(409, 'Only a task with an active assignment can start.')
    if item.status == 'IN_PROGRESS':
        s.event(db, item, user, 'MAINTENANCE_STARTED', 'Maintenance work resumed.')
    else:
        s.transition(db, item, user, 'IN_PROGRESS', 'MAINTENANCE_STARTED', 'Maintenance work started.')
    task.status = 'IN_PROGRESS'
    db.commit()
    return task_output(task, item)


@router.post('/maintenance/tasks/{key}/complete', dependencies=write)
def complete(key: UUID, values: schemas.Completion, db: Session = Depends(get_db), user=Depends(get_active_user)):
    permission(user, 'MAINTENANCE_UPDATE')
    task, item = task_record(db, key, user, lock=True)
    if task.status != 'IN_PROGRESS':
        raise HTTPException(409, 'Start the task before completing it.')
    today = business_today()
    if not task.created_at.astimezone(BUSINESS_TIMEZONE).date() <= values.completedAt <= today:
        raise HTTPException(422, 'Completion date must be between assignment and today.')
    completed = now() if values.completedAt == today else datetime.combine(values.completedAt, time.max, BUSINESS_TIMEZONE)
    s.resolve(db, item, user, values, task, completed)
    db.commit()
    return task_output(task, item)


@router.patch('/maintenance/tasks/{key}/status', dependencies=write)
def task_status(key: UUID, values: schemas.Transition, db: Session = Depends(get_db), user=Depends(get_active_user)):
    s.manager(user, 'ASSIGN')
    task, item = task_record(db, key, user, lock=True)
    if values.status != 'CANCELLED' or task.status not in ('PENDING','ASSIGNED'):
        raise HTTPException(409, 'Only a pending or assigned task can be cancelled.')
    previous = item.status
    task.status = 'CANCELLED'
    item.status, item.assigned_to, item.assigned_name = 'ESCALATED', None, None
    s.event(db, item, user, 'MAINTENANCE_CANCELLED', 'Maintenance task cancelled and returned for reassignment.', previous)
    db.commit()
    return task_output(task, item)
