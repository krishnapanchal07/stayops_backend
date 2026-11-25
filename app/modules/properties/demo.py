"""Explicit development-only demo seeder. All records and images go through HTTP APIs.

Run from Backend: python -m app.modules.properties.demo
Uses an existing Super Admin login; never creates or resets accounts.
"""
import getpass
from pathlib import Path
import httpx
from app.core.config import get_settings

PHOTOS = {
    'exterior': (5504654, 'https://www.pexels.com/photo/an-apartment-building-5504654/'),
    'bedroom': (12222276, 'https://www.pexels.com/photo/bedroom-interior-with-minimal-design-12222276/'),
    'shared-room': (5137980, 'https://www.pexels.com/photo/a-room-in-a-hostel-5137980/'),
    'kitchen': (18470956, 'https://www.pexels.com/photo/a-kitchen-in-an-apartment-18470956/'),
    'living-balcony': (19857226, 'https://www.pexels.com/photo/opened-doors-for-balcony-in-living-room-19857226/'),
}
DEMOS = [
    ('DEMO Cedar Courtyard PG','PG',['exterior','bedroom','kitchen','living-balcony']),
    ('DEMO Willow Shared Hostel','HOSTEL',['shared-room','exterior','kitchen','bedroom']),
    ('DEMO Amber Co-living','CO_LIVING',['living-balcony','bedroom','kitchen','exterior']),
]


def seed(client):
    if get_settings().app_env != 'development':
        raise RuntimeError('Demo seeding is disabled outside development.')
    folder = Path('storage/demo-sources')
    folder.mkdir(parents=True,exist_ok=True)
    for name,(number,_) in PHOTOS.items():
        file = folder / (name+'.jpg')
        if not file.exists():
            response = httpx.get(f'https://images.pexels.com/photos/{number}/pexels-photo-{number}.jpeg?w=1800&fit=max',timeout=60)
            response.raise_for_status()
            if len(response.content) >= 4*1024*1024:
                raise RuntimeError('Demo photograph exceeds upload limit.')
            file.write_bytes(response.content)
    results = []
    for name,kind,photos in DEMOS:
        response = client.get('/properties',params={'query':name,'limit':100})
        response.raise_for_status()
        matches = [p for p in response.json()['items'] if p['name']==name]
        if matches:
            item = matches[0]
            if not item['isDemo'] or item['address'] != 'DEMO address — fictional development accommodation':
                raise RuntimeError('Existing user record has a matching name; refusing to overwrite.')
        else:
            response = client.post('/properties',json=dict(name=name,type=kind,address='DEMO address — fictional development accommodation',
                city='Ahmedabad',state='Gujarat',description='DEMO only. Fictional accommodation for StayOps development. Licensed stock photographs illustrate rooms and shared spaces; they do not depict this fictional address.',
                amenities=['Wi-Fi','Laundry','Study Table','Housekeeping'],isDemo=True,isPublic=True))
            response.raise_for_status()
            item = response.json()
        response = client.post(f"/properties/{item['id']}/images",files=[('files',(name+'.jpg',(folder/(name+'.jpg')).read_bytes(),'image/jpeg')) for name in photos])
        response.raise_for_status()
        results.append({'id':item['id'],'name':name,'images':len(response.json())})
    return results


if __name__ == '__main__':
    if get_settings().app_env != 'development':
        raise SystemExit('Demo seeding is disabled outside development.')
    email = input('Existing Super Admin email: ').strip()
    password = getpass.getpass('Password: ')
    with httpx.Client(base_url='http://localhost:8000/api/v1',headers={'Origin':get_settings().frontend_url,'X-StayOps-CSRF':'1'},timeout=120) as client:
        response = client.post('/auth/login',json={'email':email,'password':password})
        response.raise_for_status()
        if response.json()['role'] != 'SUPER_ADMIN':
            raise SystemExit('Super Admin login required.')
        client.headers['Authorization'] = 'Bearer '+response.json()['access_token']
        for result in seed(client):
            print(result)
        client.post('/auth/logout')
