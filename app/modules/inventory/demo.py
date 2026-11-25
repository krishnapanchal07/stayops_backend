"""Explicit development-only API seeding; never creates tenants or allocations."""
import getpass
from pathlib import Path
import httpx
from app.core.config import get_settings
from app.modules.properties.demo import DEMOS

def seed(client):
    if get_settings().app_env!='development':
        raise RuntimeError('Inventory demo seeding is development-only.')
    results=[]
    for name,_,_ in DEMOS:
        response=client.get('/properties',params={'query':name,'limit':100});response.raise_for_status()
        matches=[p for p in response.json()['items'] if p['name']==name and p['isDemo'] and p['address']=='DEMO address — fictional development accommodation']
        if len(matches)!=1:
            raise RuntimeError('Run the property demo seeder first; ambiguous records are never overwritten.')
        prop=matches[0]
        response=client.get(f"/properties/{prop['id']}/buildings");response.raise_for_status()
        buildings=[b for b in response.json() if b['buildingCode']=='DEMO-COURT']
        if not buildings:
            response=client.post(f"/properties/{prop['id']}/buildings",json={'name':'DEMO Courtyard Wing','buildingCode':'DEMO-COURT','description':'Reusable fictional development inventory.'});response.raise_for_status();buildings=[response.json()]
        b=buildings[0]
        response=client.get(f"/buildings/{b['id']}/floors");response.raise_for_status()
        floors=[f for f in response.json() if f['floorNumber']==0]
        if not floors:
            response=client.post(f"/buildings/{b['id']}/floors",json={'name':'DEMO Garden Floor','floorNumber':0});response.raise_for_status();floors=[response.json()]
        f=floors[0]
        response=client.get('/rooms',params={'floorId':f['id'],'query':'DEMO-101','limit':100});response.raise_for_status()
        rooms=[r for r in response.json()['items'] if r['roomNumber']=='DEMO-101']
        if not rooms:
            response=client.post('/rooms',json=dict(propertyId=prop['id'],buildingId=b['id'],floorId=f['id'],roomNumber='DEMO-101',roomType='STANDARD',sharingType='2_SHARING',capacity=2,monthlyRent=8500,securityDeposit=8500,amenities=['Wi-Fi'],description='Fictional reusable demo room. Licensed photographs are illustrative.'));response.raise_for_status();rooms=[response.json()]
        room=rooms[0]
        # Existing rooms and their gallery edits are intentionally preserved.
        if not room['images']:
            files=[('files',(photo+'.jpg',Path('storage/demo-sources',photo+'.jpg').read_bytes(),'image/jpeg')) for photo in ('bedroom','shared-room')]
            response=client.post(f"/rooms/{room['id']}/images",files=files);response.raise_for_status()
        results.append({'property':name,'roomId':room['id']})
    return results

if __name__=='__main__':
    if get_settings().app_env!='development':
        raise SystemExit('Inventory demo seeding is development-only.')
    email=input('Existing Super Admin email: ').strip()
    password=getpass.getpass('Password: ')
    with httpx.Client(base_url='http://localhost:8000/api/v1',headers={'Origin':get_settings().frontend_url,'X-StayOps-CSRF':'1'},timeout=120) as client:
        response=client.post('/auth/login',json={'email':email,'password':password});response.raise_for_status()
        client.headers['Authorization']='Bearer '+response.json()['access_token']
        print(seed(client))
