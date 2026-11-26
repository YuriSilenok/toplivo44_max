"""Run with a selected bot EnvironmentFile loaded, for administrator operations."""
import argparse
import json
import secrets
import time
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('kind',choices=['fuel','wifi','labor'])
p.add_argument('action',choices=['invite','import-wifi','import-fuel','status','grant-admin'])
p.add_argument('--user-id')
p.add_argument('--file')
p.add_argument('--minutes',type=int,default=30)
a=p.parse_args()
import os
for line in Path('/etc/max-bots/'+a.kind+'.env').read_text().splitlines():
    key,_,value=line.partition('=')
    os.environ[key]=value
from app import Invite, Point, Inbox, Outbox, Role, db
if a.action=='grant-admin':
    if not a.user_id or not a.user_id.isdigit() or int(a.user_id)<=0:
        raise ValueError('Explicit recipient MAX user ID required')
    from app import CaseMessage, Session, send, menu
    with db.atomic():
        _,created=Role.get_or_create(uid=a.user_id,role='admin')
        if created:
            Session.delete().where(Session.uid==a.user_id).execute()
            send(a.user_id,'Вам назначена роль администратора. Доступные действия — в меню ниже.',menu(a.user_id))
            if a.kind=='labor':
                for pending in CaseMessage.select().where(CaseMessage.delivered==False):
                    send(a.user_id,pending.body,attachments=json.loads(pending.media))
                    pending.delivered=True
                    pending.save()
    print(a.kind,'administrator',a.user_id,'assigned' if created else 'already assigned')
elif a.action=='invite':
    token=secrets.token_urlsafe(24)
    Invite.create(token=token,role='admin',expires=time.time()+a.minutes*60)
    print('https://max.ru/'+os.environ['BOT_USERNAME']+'?start=inv_'+token)
elif a.action=='import-wifi':
    if a.kind!='wifi': raise ValueError('Choose wifi')
    rows=json.loads(Path(a.file).read_text())
    with db.atomic():
        for row in rows:
            lat,lon=float(row['latitude']),float(row['longitude'])
            if not (-90<=lat<=90 and -180<=lon<=180): raise ValueError('Invalid coordinates')
            source='import:'+str(row['id'])
            values=dict(name=row['name'],address=row['address'],lat=lat,lon=lon,extra=json.dumps({k:row.get(k,'') for k in ['ssid','password','comment']},ensure_ascii=False))
            old=Point.get_or_none(Point.source==source)
            Point.update(**values).where(Point.id==old.id).execute() if old else Point.create(source=source,**values)
    print('Imported',len(rows),'Wi-Fi points')
elif a.action=='import-fuel':
    if a.kind!='fuel': raise ValueError('Choose fuel')
    from portal import ingest
    print('Imported',ingest(json.loads(Path(a.file).read_text())),'fuel records')
else:
    print(a.kind,'points',Point.select().count(),'admins',Role.select().where(Role.role=='admin').count(),'pending events',Inbox.select().where(Inbox.done==False).count(),'outbox',Outbox.select().count())
