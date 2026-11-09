"""Explicit, all-or-nothing trash restoration. Never processes or sends a PDF."""
import os,sys,json,datetime,uuid
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine,text

def main():
    if os.getenv('PROJECT1_TRASH_RESTORE_APPROVAL')!='RESTORE: historical trash':
        raise RuntimeError('Separate owner approval is required; no changes made')
    snapshot=json.load(sys.stdin);cutoff=datetime.datetime(2026,10,25,tzinfo=ZoneInfo('Europe/Moscow'))
    records=[]
    for row in snapshot['records']:
        date=datetime.datetime.fromisoformat(row['message']['received_at'])
        if date.tzinfo is None:date=date.replace(tzinfo=datetime.timezone.utc)
        if date<cutoff:records.append(row['attachment'])
    if len(records)!=240 or len({r['id'] for r in records})!=240:raise RuntimeError('Invalid trash manifest')
    engine=create_engine(os.environ['DATABASE_URL'],hide_parameters=True)
    with engine.begin() as db:
        for old in sorted(records,key=lambda r:r['id']):
            row=db.execute(text('SELECT status,current_revision_id,file_sha256 FROM attachments WHERE id=:id FOR UPDATE'),{'id':uuid.UUID(old['id'])}).one()
            if row.status!='archived' or row.current_revision_id is not None or row.file_sha256!=old['file_sha256']:
                raise RuntimeError('Trash changed: restore refused without overwriting newer work')
            db.execute(text('UPDATE attachments SET status=:status WHERE id=:id'),{'status':old['status'],'id':uuid.UUID(old['id'])})
    engine.dispose();print(json.dumps({'restored_from_trash':240,'processing_queued':0,'smtp_attempts':0}))
if __name__=='__main__':main()
