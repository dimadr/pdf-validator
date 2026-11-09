"""UID-safe IMAP ingestion with PEEK, durable MIME and completion-only Seen flags."""
import os,ssl,imaplib,email,uuid,hashlib,pathlib,tempfile,json
from email import policy
from email.header import decode_header
from email.utils import parseaddr,parsedate_to_datetime
from datetime import datetime,timezone
from sqlalchemy import select,func
from common.models import MailboxCheckpoint,IncomingMessage,EmailSource,Attachment,RecipientDelivery,DeliveryIntent,ProcessingRevision,MailboxAction
from common.config import require_schema
from common.service import request_reprocess
from common.delivery import now,ServiceConflict

class BlobStore:
    def __init__(self,root):
        self.root=pathlib.Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)

    def put(self,content,suffix):
        digest=hashlib.sha256(content).hexdigest();destination=self.root/(digest+suffix)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest()!=digest:raise ValueError('Stored blob integrity conflict')
            return str(destination),digest
        handle,name=tempfile.mkstemp(prefix='.staging-',dir=self.root)
        try:
            with os.fdopen(handle,'wb') as out:
                out.write(content);out.flush();os.fsync(out.fileno())
            os.replace(name,destination)
            if os.name!='nt':
                directory=os.open(self.root,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)
        return str(destination),digest

def safe_header(value):
    if not value:return ''
    result=[]
    for text,charset in decode_header(str(value)):
        if isinstance(text,bytes):
            try:text=text.decode(charset or 'utf8',errors='replace')
            except (LookupError,UnicodeError):text=text.decode('utf8',errors='replace')
        result.append(text)
    return ''.join(result)

def mime_manifest(raw):
    message=email.message_from_bytes(raw,policy=policy.compat32)
    # A receipt is recognized structurally; normal PDF replies are never discarded by subject.
    receipt=message.get_content_type()=='multipart/report' and message.get_param('report-type')=='disposition-notification'
    parts=[]
    for index,part in enumerate(message.walk()):
        if part.is_multipart():continue
        filename=safe_header(part.get_filename())
        if not (part.get_content_type()=='application/pdf' or filename.lower().endswith('.pdf')):continue
        content=part.get_payload(decode=True)
        if content is None:
            parts.append({'part_index':index,'filename':filename or 'document.pdf','state':'decode_error','content':None})
            continue
        parts.append({'part_index':index,'filename':pathlib.Path(filename.replace('\\','/')).name or 'document.pdf',
            'sha256':hashlib.sha256(content).hexdigest(),'size':len(content),'content':content})
    return message,parts,receipt

def ingest_uid(session_factory,store,account,mailbox,uidvalidity,uid,raw,*,matched_receipt_only=False):
    """Persist raw first, manifest before child work; cursor advances only with complete manifest."""
    raw_path=None
    if not matched_receipt_only:
        raw_path,_=store.put(raw,'.eml')
    message,parts,receipt=mime_manifest(raw)
    if receipt:
        from common.mailru_receipts import match_mailru_delivery
        from common.mailbox_archive import queue_move,NOTIFICATIONS
        matched=0
        with session_factory() as db:
            with db.begin():
                for part in message.walk():
                    if part.get_content_type()!='message/disposition-notification':continue
                    for report in part.get_payload() if isinstance(part.get_payload(),list) else []:
                        disposition=str(report.get('Disposition','')).split(';')[-1].strip().split('/')[0].lower()
                        mid=str(report.get('Original-Message-ID','')).strip()
                        recipient=str(report.get('Final-Recipient','')).split(';')[-1].strip()
                        if disposition!='displayed' or not recipient:continue
                        if mid:
                            match=RecipientDelivery.message_id==mid
                        else:
                            delivery_id=match_mailru_delivery(db,message,raw,account,recipient)
                            if delivery_id is None:continue
                            match=RecipientDelivery.id==delivery_id
                        attachment=db.scalar(select(Attachment).join(ProcessingRevision,ProcessingRevision.attachment_id==Attachment.id)
                            .join(DeliveryIntent,DeliveryIntent.revision_id==ProcessingRevision.id)
                            .join(RecipientDelivery,RecipientDelivery.intent_id==DeliveryIntent.id).where(
                                match,RecipientDelivery.recipient==recipient,
                                RecipientDelivery.state=='accepted').with_for_update(of=Attachment))
                        if attachment is not None:
                            attachment.read_receipt_received=True
                            if attachment.read_receipt_at is None:attachment.read_receipt_at=now()
                            matched+=1
                if matched or not matched_receipt_only:
                    if raw_path is None:raw_path,_=store.put(raw,'.eml')
                    queue_move(db,account,mailbox,uidvalidity,uid,NOTIFICATIONS,raw_path,safe_header(message.get('Message-ID')) or None)
        return {'state':'complete','receipt':True,'matched':matched}
    if matched_receipt_only:
        return {'state':'skipped','receipt':False}
    sender=parseaddr(safe_header(message.get('From')))[1]
    if '@' in sender:
        local,domain=sender.rsplit('@',1);sender=local+'@'+domain.lower()
    try:received=parsedate_to_datetime(message.get('Date'))
    except (ValueError,TypeError,IndexError,OverflowError):received=now()
    if received.tzinfo is None:received=received.replace(tzinfo=timezone.utc)
    with session_factory() as db:
        with db.begin():
            require_schema(db)
            db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(hashlib.sha256((account+'\0'+mailbox).encode()).digest()[:8],'big',signed=True))))
            existing=db.scalar(select(IncomingMessage).where(IncomingMessage.imap_account==account,IncomingMessage.imap_mailbox==mailbox,
                IncomingMessage.imap_uidvalidity==uidvalidity,IncomingMessage.imap_uid==uid))
            if existing and existing.ingest_state=='complete':return {'state':'existing','message_id':str(existing.id)}
            sources=db.scalars(select(EmailSource).where(EmailSource.is_active.is_(True))).all()
            source=next((s for s in sources if parseaddr(s.email)[1].rsplit('@',1)[0]+'@'+parseaddr(s.email)[1].rsplit('@',1)[-1].lower()==sender),None)
            if source is None:
                # Quarantined raw is retained; unauthorized mail advances the cursor without creating a trusted source.
                return {'state':'untrusted','raw_path':raw_path}
            if existing is None:
                existing=IncomingMessage(id=uuid.uuid4(),source_id=source.id,provider_message_id=safe_header(message.get('Message-ID'))[:255] or None,
                    from_email=sender,subject=safe_header(message.get('Subject')),received_at=received,status='new',
                    imap_account=account,imap_mailbox=mailbox,imap_uidvalidity=uidvalidity,imap_uid=uid,ingest_state='manifest',raw_mime_path=raw_path,
                    part_manifest=[{k:v for k,v in p.items() if k!='content'} for p in parts])
                db.add(existing);db.flush()
            existing_id=existing.id
    # Each part completes independently. A failed part does not prevent the next part from being retained.
    failures=[]
    for part in parts:
        try:
            if part['content'] is None:raise ValueError('Undecodable MIME PDF retained in raw message')
            digest=part['sha256']
            with session_factory() as db:
                with db.begin():
                    db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(bytes.fromhex(digest)[:8],'big',signed=True))))
                    attachment=db.scalar(select(Attachment).where(Attachment.file_sha256==digest))
                    if attachment is None:
                        pdf_path,_=store.put(part['content'],'.pdf')
                        attachment=Attachment(id=uuid.uuid4(),message_id=existing_id,filename=part['filename'][:255],file_path=pdf_path,
                            file_sha256=digest,file_size=part['size'],status='new')
                        db.add(attachment);db.flush()
                        request_reprocess(db,attachment.id,'ingest:'+str(existing_id)+':'+str(part['part_index']))
                        disposition='saved'
                    else:
                        current=db.get(ProcessingRevision,attachment.current_revision_id) if attachment.current_revision_id else None
                        pdf_path=attachment.file_path
                        if current and current.state=='discarded_inactive':disposition='discarded_inactive'
                        elif not pdf_path or not pathlib.Path(pdf_path).is_file():disposition='historical_content_unavailable'
                        else:
                            if hashlib.sha256(pathlib.Path(pdf_path).read_bytes()).hexdigest()!=digest:
                                raise ValueError('Existing attachment integrity conflict')
                            disposition='deduplicated'
                    parent=db.get(IncomingMessage,existing_id)
                    manifest=[dict(p) for p in parent.part_manifest]
                    for p in manifest:
                        if p['part_index']==part['part_index']:p.update(state=disposition,attachment_id=str(attachment.id),file_path=pdf_path,raw_part_available=True)
                    parent.part_manifest=manifest
        except Exception:
            failures.append(part['part_index'])
    with session_factory() as db:
        with db.begin():
            parent=db.get(IncomingMessage,existing_id)
            parent.ingest_state='incomplete' if failures else 'complete'
            if failures:parent.error_message='MIME parts require recovery: '+','.join(map(str,failures))
            else:parent.error_message=None;parent.processed_at=now()
    return {'state':'incomplete' if failures else 'complete','message_id':str(existing_id),'failures':failures,'receipt':receipt}

class IMAPSource:
    def __init__(self,connection=None):
        self.connection=connection or imaplib.IMAP4_SSL(os.environ['IMAP_SERVER'],int(os.getenv('IMAP_PORT','993')),
            ssl_context=ssl.create_default_context(),timeout=20)
        self.connection.login(os.environ['IMAP_USER'],os.environ['IMAP_PASSWORD'])
    def select(self,mailbox):
        status,_=self.connection.select(mailbox,readonly=True)
        if status!='OK':raise RuntimeError('IMAP SELECT failed')
        _,values=self.connection.response('UIDVALIDITY')
        if not values or values[0] is None:raise RuntimeError('IMAP UIDVALIDITY missing')
        return int(values[0])
    def uids_after(self,last_uid):
        status,data=self.connection.uid('SEARCH',None,'UID',str(last_uid+1)+':*')
        if status!='OK':raise RuntimeError('IMAP UID SEARCH failed')
        return sorted(int(v) for v in data[0].split() if int(v)>last_uid)
    def fetch(self,uid):
        status,items=self.connection.uid('FETCH',str(uid),'(BODY.PEEK[])')
        if status!='OK':raise RuntimeError('IMAP UID FETCH failed')
        raw=next((item[1] for item in items if isinstance(item,tuple)),None)
        if raw is None:raise RuntimeError('IMAP body missing')
        return raw
    def unread_uids(self):
        status,data=self.connection.uid('SEARCH',None,'UNSEEN')
        if status!='OK':raise RuntimeError('IMAP UNSEEN search failed')
        return [int(v) for v in data[0].split()]
    def mark_seen(self,uid,mailbox,uidvalidity):
        try:
            status,_=self.connection.select(mailbox,readonly=False)
            if status!='OK':raise RuntimeError('IMAP writable SELECT failed')
            _,values=self.connection.response('UIDVALIDITY')
            if not values or values[0] is None or int(values[0])!=uidvalidity:
                raise RuntimeError('UIDVALIDITY changed before marking Seen')
            status,_=self.connection.uid('STORE',str(uid),'+FLAGS.SILENT',r'(\Seen)')
            return status=='OK'
        finally:self.select(mailbox)
    def close(self):
        try:self.connection.logout()
        except Exception:pass

def poll(session_factory,source,store,account,mailbox='INBOX',batch=100):
    validity=source.select(mailbox)
    # A session advisory lock prevents two pollers from moving a checkpoint backwards.
    lock_key=int.from_bytes(hashlib.sha256(('poll\0'+account+'\0'+mailbox).encode()).digest()[:8],'big',signed=True)
    with session_factory() as guard:
        acquired=guard.scalar(select(func.pg_try_advisory_lock(lock_key)))
        if not acquired:return {'state':'busy'}
        try:
            with session_factory() as db:
                with db.begin():
                    require_schema(db)
                    checkpoint=db.scalar(select(MailboxCheckpoint).where(MailboxCheckpoint.account==account,MailboxCheckpoint.mailbox==mailbox).with_for_update())
                    if checkpoint is None:
                        initial=os.getenv('IMAP_BOOTSTRAP_UID')
                        if initial is None and db.scalar(select(Attachment.id).where(Attachment.current_revision_id.is_(None)).limit(1)):
                            return {'state':'bootstrap_required','reason':'Explicit cutover UID required for legacy history; historical backfill is not automatically authorized'}
                        initial=int(initial or '0')
                        if initial<0:raise ValueError('IMAP_BOOTSTRAP_UID must be nonnegative')
                        checkpoint=MailboxCheckpoint(id=uuid.uuid4(),account=account,mailbox=mailbox,uidvalidity=validity,last_uid=initial);db.add(checkpoint)
                    elif checkpoint.uidvalidity!=validity:checkpoint.uidvalidity=validity;checkpoint.last_uid=0
                    cursor=checkpoint.last_uid
            processed=0
            for uid in source.uids_after(cursor)[:batch]:
                result=ingest_uid(session_factory,store,account,mailbox,validity,uid,source.fetch(uid))
                # The raw MIME and complete part manifest are durable even when a child needs recovery.
                # Reconciliation below resumes that child; do not starve later mailbox UIDs.
                with session_factory() as db:
                    with db.begin():
                        checkpoint=db.scalar(select(MailboxCheckpoint).where(MailboxCheckpoint.account==account,MailboxCheckpoint.mailbox==mailbox).with_for_update())
                        checkpoint.last_uid=max(checkpoint.last_uid,uid)
                processed+=1
            return {'state':'complete','processed':processed}
        finally:
            guard.execute(select(func.pg_advisory_unlock(lock_key)));guard.commit()

def mark_completed_seen(session_factory,source,account,mailbox='INBOX',limit=100):
    """Only fully accepted current client operations; pending/review/failed mail stays unread."""
    from common.delivery_status import client_delivery
    validity=source.select(mailbox);unread=source.unread_uids()
    if not unread:return {'marked':0,'errors':0}
    eligible=[]
    with session_factory() as db:
        with db.begin():
            messages=db.scalars(select(IncomingMessage).where(IncomingMessage.imap_account==account,
                IncomingMessage.imap_mailbox==mailbox,IncomingMessage.imap_uidvalidity==validity,
                IncomingMessage.imap_uid.in_(unread),IncomingMessage.ingest_state=='complete').order_by(IncomingMessage.imap_uid)).all()
            for message in messages:
                parts=message.part_manifest or []
                if not parts or any(not p.get('attachment_id') for p in parts):continue
                complete=True
                for part in parts:
                    attachment=db.get(Attachment,uuid.UUID(part['attachment_id']))
                    revision=db.get(ProcessingRevision,attachment.current_revision_id) if attachment and attachment.current_revision_id else None
                    if not revision or attachment.status=='archived' or revision.state!='validated' or not client_delivery(db,revision.id)['all_accepted']:
                        complete=False;break
                if complete:eligible.append(message.imap_uid)
                if len(eligible)>=limit:break
    marked=errors=0
    for uid in eligible:
        try:
            if source.mark_seen(uid,mailbox,validity):marked+=1
            else:errors+=1
        except Exception:errors+=1
    return {'marked':marked,'errors':errors}

def recover_incomplete(session_factory,store,limit=100):
    with session_factory() as db:
        rows=db.scalars(select(IncomingMessage).where(IncomingMessage.ingest_state.in_(['manifest','incomplete']))
            .order_by(IncomingMessage.received_at).limit(limit)).all()
        work=[(m.imap_account,m.imap_mailbox,m.imap_uidvalidity,m.imap_uid,m.raw_mime_path) for m in rows]
    recovered=0
    for account,mailbox,validity,uid,path in work:
        if path and pathlib.Path(path).is_file():
            result=ingest_uid(session_factory,store,account,mailbox,validity,uid,pathlib.Path(path).read_bytes())
            recovered+=result['state']=='complete'
    return recovered

def reconcile_files(session_factory,store):
    """Report orphan/staging content; never delete received bytes automatically."""
    with session_factory() as db:
        referenced={str(pathlib.Path(p).resolve()) for p in db.scalars(select(Attachment.file_path)).all() if p}
        referenced.update(str(pathlib.Path(p).resolve()) for p in db.scalars(select(IncomingMessage.raw_mime_path)).all() if p)
        referenced.update(str(pathlib.Path(p).resolve()) for p in db.scalars(select(MailboxAction.raw_path)).all() if p)
    return {'unreferenced':[str(p) for p in store.root.iterdir() if p.is_file() and str(p.resolve()) not in referenced],
        'missing':[p for p in referenced if not pathlib.Path(p).is_file()]}
