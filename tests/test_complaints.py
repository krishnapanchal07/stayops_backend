"""Complaint workflow regressions on the disposable PostgreSQL fixture."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from uuid import uuid4

from PIL import Image
from sqlalchemy import func, select

from app.modules.auth.models import PropertyAssignment, User
from app.modules.complaints.models import Complaint, Task
from app.modules.finance.business_date import business_today
from app.modules.staff.models import StaffProfile
from app.modules.tenants.schemas import TenantWrite
from app.modules.tenants.service import create
from conftest import authorization, sign_in

ROOT = '/api/v1/complaints'
TASKS = '/api/v1/maintenance/tasks'
PAYLOAD = dict(title='Water leak', category='PLUMBING', priority='URGENT',
    description='There is water leaking from the bathroom tap.')


def fixture(setup):
    client, factory, *_ = setup
    with factory() as db:
        def user(email):
            return db.scalar(select(User).where(User.email == email))
        admin = user('admin@example.com')
        account = user('tenant@example.com')
        tenant = create(db, TenantWrite(firstName='Phase Six', lastName='QA', email=account.email,
            phone='9876543210', propertyId='prop-sunrise', roomId='room-sunrise-205',
            bedId='bed-205-b', moveInDate=business_today(), status='ACTIVE'), admin, commit=False)
        tenant.user_id = account.id
        techs = {}
        for name, site in [('maintenance','prop-sunrise'), ('other','prop-greenview')]:
            tech = User(full_name=name.title(), role_id='MAINTENANCE',
                email=f'{name}@example.com', password_hash=admin.password_hash)
            db.add(tech)
            db.flush()
            db.add(StaffProfile(user_id=tech.id, phone='9000000000', department='MAINTENANCE', status='ACTIVE',
                designation='Technician', joining_date=business_today(), address={}, emergency_contact={}))
            db.add(PropertyAssignment(user_id=tech.id, property_id=site))
            techs[name] = str(tech.id)
        db.commit()
    return client, factory, dict(
        admin=authorization(sign_in(client,'admin@example.com')), tenant=authorization(sign_in(client)),
        owner=authorization(sign_in(client,'owner@example.com')),
        technician=authorization(sign_in(client,'maintenance@example.com')),
        other=authorization(sign_in(client,'other@example.com'))), techs


def submit(client, headers, payload=PAYLOAD, key=None, files=None):
    return client.post(ROOT, headers=headers | {'Idempotency-Key': key or uuid4().hex},
        data={'payload': json.dumps(payload)}, files=files)


def test_complaint_workflow_and_scope(setup, monkeypatch, tmp_path):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), 'property_image_directory', str(tmp_path))
    c, factory, h, techs = fixture(setup)
    image = BytesIO()
    Image.new('RGB', (10, 10), 'blue').save(image, format='PNG')
    files=[('files', ('leak.png', image.getvalue(), 'image/png'))]
    assert submit(c,h['tenant'],PAYLOAD | {'tenantId':str(uuid4())}).status_code == 422
    assert submit(c,h['tenant'],PAYLOAD | {'roomId':'other-room'}).status_code == 422
    assert submit(c,h['tenant'],files=[('files',('bad.png', b'not an image', 'image/png'))]).status_code == 400
    response = submit(c,h['tenant'],key='test-duplicate-key',files=files)
    assert response.status_code == 201, response.text
    item = response.json()
    assert submit(c,h['tenant'],key='test-duplicate-key',files=files).json()['id'] == item['id']
    url = ROOT + '/' + item['id']
    assert item['status'] == 'OPEN' and item['roomNumber'] == '205'
    assert c.get(url,headers=h['owner']).status_code == 404
    assert c.get(url,headers=h['other']).status_code == 403
    attachment = f"{url}/attachments/{item['attachments'][0]['id']}"
    assert c.get(attachment).status_code == 401
    assert c.get(attachment,headers=h['owner']).status_code == 404
    assert c.get(attachment,headers=h['tenant']).content[:4] == b'RIFF'
    assert submit(c, h['tenant'], key='test-duplicate-key').status_code == 409
    room_options = c.get(ROOT + '/options/tenants', params={'propertyId': 'prop-sunrise',
        'roomId': 'room-sunrise-205'}, headers=h['admin']).json()
    assert [row['id'] for row in room_options['items']] == [item['tenantId']]
    assert c.get(ROOT + '/options/tenants', params={'propertyId': 'prop-greenview',
        'roomId': 'room-sunrise-205'}, headers=h['admin']).json()['items'] == []
    staff_options = c.get(ROOT + '/options/staff', params={'propertyId': 'prop-sunrise'}, headers=h['admin']).json()
    assert [row['id'] for row in staff_options['items']] == [techs['maintenance']]
    assert c.get(ROOT,params={'query':'WATER','limit':1},headers=h['admin']).json()['total'] == 1
    assert c.get(ROOT+'/summary',headers=h['tenant']).json()['urgent'] == 1
    assert c.post(url+'/comments',json={'message':'Private note','visibility':'INTERNAL'},headers=h['tenant']).status_code == 403
    assert c.post(url+'/comments',json={'message':'Private note','visibility':'INTERNAL'},headers=h['admin']).status_code == 200
    assert c.post(url+'/comments',json={'message':'A public update'},headers=h['admin']).status_code == 200
    assert {x['visibility'] for x in c.get(url + '/comments', headers=h['admin']).json()} == {'PUBLIC', 'INTERNAL'}
    comments = c.get(url+'/comments',headers=h['tenant']).json()
    assert [x['message'] for x in comments] == ['A public update']
    assert comments[0]['authorName'] == 'Property team'
    assert c.patch(url+'/status',json={'status':'REVIEWED'},headers=h['admin']).status_code == 200
    assert c.post(url+'/assign',json={'staffId':techs['other']},headers=h['admin']).status_code == 422
    def assign(_):
        return c.post(url+'/assign',json={'staffId':techs['maintenance']},headers=h['admin'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assignments = list(pool.map(assign, range(2)))
    assert all(r.status_code == 200 for r in assignments), [r.text for r in assignments]
    task = assignments[0].json()['task']
    assert assignments[1].json()['task']['id'] == task['id']
    task_url = TASKS + '/' + task['id']
    assert c.get(task_url,headers=h['other']).status_code == 404
    assert c.get(task_url,headers=h['tenant']).status_code == 403
    assert c.post(task_url+'/start',headers=h['technician']).json()['status'] == 'IN_PROGRESS'
    assert c.get(url,headers=h['tenant']).json()['status'] == 'IN_PROGRESS'
    completion = {'resolutionNotes':'Replaced washer', 'actualCost':'150.50', 'partsUsed':'Washer',
        'completedAt':str(business_today())}
    assert c.post(task_url+'/complete',json=completion,headers=h['technician']).json()['status'] == 'COMPLETED'
    assert c.post(task_url+'/complete',json=completion,headers=h['technician']).status_code == 409
    assert c.get(url,headers=h['tenant']).json()['actualCost'] is None
    assert c.post(url+'/reopen',headers=h['tenant']).json()['status'] == 'IN_PROGRESS'
    assert c.post(url+'/resolve',json=completion | {'partsUsed':None},headers=h['admin']).status_code == 422
    assert c.post(url+'/resolve',json={'resolutionNotes':'Checked the repair again'},headers=h['admin']).status_code == 200
    assert c.post(url+'/close',headers=h['tenant']).json()['status'] == 'CLOSED'
    assert [row['type'] for row in c.get(url+'/timeline',headers=h['tenant']).json()][-3:] == ['REOPENED','RESOLVED','CLOSED']
    assert c.post(f"/api/v1/tenants/{item['tenantId']}/checkout",json={'moveOutDate':str(business_today())},headers=h['admin']).status_code == 200
    assert submit(c,h['tenant']).status_code == 409
    assert c.get(url,headers=h['tenant']).json()['roomNumber'] == '205'
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Complaint)) == 1
        assert db.scalar(select(func.count()).select_from(Task)) == 2
