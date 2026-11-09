"""Durable, UID-scoped IMAP MOVE; transport uncertainty never schedules SMTP."""
import base64,hashlib,pathlib,re,uuid
from email.utils import getaddresses
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from common.models import MailboxAction,IncomingMessage,Attachment,ProcessingRevision,utcnow
from common.delivery_status import client_delivery

PROCESSED='Обработанные'
NOTIFICATIONS='Уведомления'

def folder_name(value):
    """IMAP modified UTF-7, independent of server UTF8=ACCEPT support."""
    result=[];run=[]
    def flush():
        if run:
            result.append('&'+base64.b64encode(''.join(run).encode('utf-16-be')).decode().rstrip('=').replace('/',',')+'-');run.clear()
    for char in value:
        if ' '<=char<='~':flush();result.append('&-' if char=='&' else char)
        else:run.append(char)
    flush();return ''.join(result)

def queue_move(db,account,mailbox,validity,uid,destination,raw_path,message_id=None):
    if destination not in (PROCESSED,NOTIFICATIONS) or destination==mailbox:raise ValueError('Unsupported archive destination')
    path=pathlib.Path(raw_path);digest=hashlib.sha256(path.read_bytes()).hexdigest()
    db.execute(insert(MailboxAction).values(id=uuid.uuid4(),account=account,mailbox=mailbox,uidvalidity=validity,
        uid=uid,destination=destination,raw_path=str(path),raw_sha256=digest,message_id=message_id,state='pending')
        .on_conflict_do_nothing(constraint='uq_mailbox_action'))

def _completed(db,message):
    parts=message.part_manifest or []
    if not parts or any(not p.get('attachment_id') for p in parts):return False
    for part in parts:
        attachment=db.get(Attachment,uuid.UUID(part['attachment_id']))
        revision=db.get(ProcessingRevision,attachment.current_revision_id) if attachment and attachment.current_revision_id else None
        if not revision or attachment.status=='archived' or revision.state!='validated' or not client_delivery(db,revision.id)['all_accepted']:return False
    return True

def schedule_completed(factory,source,account,mailbox='INBOX',limit=100):
    validity=source.select(mailbox)
    status,data=source.connection.uid('SEARCH',None,'ALL')
    if status!='OK':raise RuntimeError('IMAP UID search failed')
    present=[int(u) for u in data[0].split()]
    if not present:return 0
    count=0
    with factory() as db:
        with db.begin():
            messages=db.scalars(select(IncomingMessage).where(IncomingMessage.imap_account==account,
                IncomingMessage.imap_mailbox==mailbox,IncomingMessage.imap_uidvalidity==validity,
                IncomingMessage.imap_uid.in_(present),IncomingMessage.ingest_state=='complete').order_by(IncomingMessage.imap_uid)).all()
            for message in messages:
                if message.raw_mime_path and _completed(db,message):
                    queue_move(db,account,mailbox,validity,message.imap_uid,PROCESSED,message.raw_mime_path,message.provider_message_id);count+=1
                    if count>=limit:break
    return count

def _target_match(source,action):
    conn=source.connection
    try:
        status,_=conn.select(folder_name(action.destination),readonly=True)
        if status!='OK':return False
        mid=action.message_id
        if mid and not any(c in mid for c in '\r\n"\\'):
            status,data=conn.uid('SEARCH',None,'HEADER','Message-ID','"'+mid+'"')
        else:status,data=conn.uid('SEARCH',None,'ALL')
        if status!='OK':raise RuntimeError('Archive reconciliation search failed')
        for uid in data[0].split()[-100:]:
            status,items=conn.uid('FETCH',uid.decode(),'(BODY.PEEK[])')
            if status!='OK':raise RuntimeError('Archive reconciliation fetch failed')
            raw=next((r[1] for r in items if isinstance(r,tuple)),None)
            if raw is not None and hashlib.sha256(raw).hexdigest()==action.raw_sha256:return True
        return False
    finally:source.select(action.mailbox)

def _move(source,action):
    conn=source.connection;status,data=conn.capability()
    if status!='OK' or b'MOVE' not in b' '.join(data).upper().split():raise RuntimeError('IMAP MOVE capability required')
    target=folder_name(action.destination)
    status,folders=conn.list('""',target)
    if status!='OK':raise RuntimeError('Destination LIST failed')
    if not any(folders):
        status,data=conn.create(target)
        if status!='OK' and not any(b'ALREADYEXISTS' in str(row).upper().encode() for row in data):raise RuntimeError('Destination CREATE failed')
    try:
        status,_=conn.select(action.mailbox,readonly=False)
        _,values=conn.response('UIDVALIDITY')
        if status!='OK' or not values or int(values[0])!=action.uidvalidity:raise RuntimeError('UIDVALIDITY changed before MOVE')
        status,_=conn.uid('MOVE',str(action.uid),target)
        if status!='OK':raise RuntimeError('UID MOVE result not confirmed')
    finally:source.select(action.mailbox)

def reconcile_moves(factory,source,account,mailbox='INBOX',limit=100,before_commit=None):
    marked=uncertain=0
    with factory() as db:
        ids=list(db.scalars(select(MailboxAction.id).where(MailboxAction.account==account,MailboxAction.mailbox==mailbox,
            MailboxAction.state!='done').order_by(MailboxAction.created_at).limit(limit)))
    for id in ids:
        with factory() as db:
            with db.begin():
                action=db.scalar(select(MailboxAction).where(MailboxAction.id==id,MailboxAction.state!='done').with_for_update(skip_locked=True))
                if action is None:continue
                try:
                    if source.select(mailbox)!=action.uidvalidity:raise RuntimeError('UIDVALIDITY changed; hold archive operation')
                    status,data=source.connection.uid('SEARCH',None,'UID',str(action.uid))
                    if status!='OK':raise RuntimeError('Source search failed')
                    present=str(action.uid).encode() in data[0].split()
                    target=_target_match(source,action)
                    if not present and target:action.state='done'
                    elif action.state=='uncertain' or target or not present:
                        raise RuntimeError('Ambiguous archive outcome; no blind MOVE retry')
                    else:
                        if hashlib.sha256(source.fetch(action.uid)).hexdigest()!=action.raw_sha256:raise RuntimeError('Source MIME integrity changed')
                        _move(source,action)
                        status,data=source.connection.uid('SEARCH',None,'UID',str(action.uid))
                        if status!='OK' or str(action.uid).encode() in data[0].split() or not _target_match(source,action):
                            raise RuntimeError('Archive postcondition not proven')
                        action.state='done'
                    action.finished_at=utcnow();action.error=None;marked+=1
                except Exception as exc:
                    action.state='uncertain';action.error=type(exc).__name__+': '+str(exc);uncertain+=1
                if before_commit:before_commit(db)
    return {'moved_or_confirmed':marked,'uncertain':uncertain}

def _queue_receipts(factory,source,store,account,mailbox,matched_only=False):
    from common.ingest import mime_manifest,ingest_uid
    validity=source.select(mailbox)
    status,data=source.connection.uid('SEARCH',None,'ALL')
    if status!='OK':raise RuntimeError('Receipt UID search failed')
    uids=data[0].split()[-(20 if matched_only else 100):]
    if not uids:return
    # The live server omits Content-Type matches in SEARCH HEADER. Read just
    # the headers of recent UIDs and fetch full MIME only for actual MDNs.
    uid_set=b','.join(uids).decode('ascii')
    status,headers=source.connection.uid('FETCH',uid_set,'(UID BODY.PEEK[HEADER.FIELDS (CONTENT-TYPE)])')
    if status!='OK':raise RuntimeError('Receipt header fetch failed')
    for item in headers:
        if not isinstance(item,tuple) or not mime_manifest(item[1])[2]:continue
        uid_field=re.search(rb'\bUID (\d+)\b',item[0])
        if not uid_field or uid_field[1] not in uids:continue
        uid=int(uid_field[1])
        raw=source.fetch(uid)
        message,_,receipt=mime_manifest(raw)
        if not receipt:continue
        if matched_only:
            addresses=getaddresses([str(value) for value in message.get_all('To',[])])
            if [address for _,address in addresses]!=[account]:continue
            ingest_uid(factory,store,account,mailbox,validity,uid,raw,matched_receipt_only=True)
        else:
            ingest_uid(factory,store,account,mailbox,validity,uid,raw)


def archive_mailbox(factory,source,store,account,mailbox='INBOX'):
    _queue_receipts(factory,source,store,account,mailbox)
    schedule_completed(factory,source,account,mailbox)
    result=reconcile_moves(factory,source,account,mailbox)
    # This mailbox provider can file received MDNs in Sent. Preserve ordinary outgoing mail.
    if mailbox!='Sent':
        status,folders=source.connection.list('""','Sent')
        if status=='OK' and any(folders):
            _queue_receipts(factory,source,store,account,'Sent',matched_only=True)
            sent=reconcile_moves(factory,source,account,'Sent')
            for key in result:result[key]+=sent[key]
    return result
