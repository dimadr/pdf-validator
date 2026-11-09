"""Pure, fail-closed legacy queue policy. Never publishes, ACKs, edits or removes."""
import collections,datetime,hashlib,json
def write_artifact(path,value):
    import pathlib
    target=path.resolve();production=pathlib.Path('/root/project1')
    if target==production or production in target.parents:raise ValueError('STOP: artifact path is inside production')
    target.parent.mkdir(exist_ok=True,parents=True);target.write_text(json.dumps(value,indent=2)+'\n');target.chmod(0o600)
SEND='email_sender.send_pdf_attachment'
CATEGORIES=('OBSOLETE','RETRY_DUPLICATE','MISSING_PDF','VALID_PENDING','SMTP_UNCERTAIN','UNKNOWN')
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def semantic_document(d):return {k:d.get(k) for k in ('attachment_id','content_sha256','status','reject_reason','pdf_exists','pdf_hash_matches','sent_at','message_id_present','accepted_recipient_count','all_expected_recorded','current_route_role','object_id','object_active')}
def classify(snapshot):
    docs={d['attachment_id']:d for d in snapshot['documents']};groups=collections.defaultdict(list);task_docs=collections.defaultdict(set)
    for t in snapshot['deliveries']:
        groups[(t.get('task'),t.get('attachment_id'))].append(t);task_docs[t.get('task_id')].add(t.get('attachment_id'))
    # --plan/--include-ids-from retains operations after their transport messages finish.
    represented={attachment for task,attachment in groups}
    for attachment in docs:
        if attachment not in represented:groups[(SEND,attachment)]=[]
    operations=[];rows=[]
    for (task,attachment),tasks in sorted(groups.items(),key=lambda item:str(item[0])):
        d=docs.get(attachment);reason='';kind='UNKNOWN'
        if task!=SEND or not d or any(not t.get('call_shape_known') or len(task_docs[t.get('task_id')])!=1 for t in tasks):reason='Unknown task/call shape/document identity'
        elif not d.get('file_path_safe'):reason='PDF path is outside the verified uploads scope; absence is not proven'
        elif d.get('smtp_uncertain') or ((d.get('sent_at') or d.get('message_id_present') or d.get('accepted_recipient_count')) and not (d.get('all_expected_recorded') and d.get('sent_at') and d.get('message_id_present'))):
            kind='SMTP_UNCERTAIN';reason='Partial/uncommitted SMTP evidence; no automatic replay'
        elif d.get('all_expected_recorded') and d.get('sent_at') and d.get('message_id_present') and d['status'] in ('sent','rejected'):
            kind='OBSOLETE';reason='Recorded acceptance covers current route; admin notice does not prove client delivery'
        elif not d.get('pdf_exists'):
            kind='MISSING_PDF';reason='Cannot construct current SMTP message; retain history and hold, never auto replay after file recovery'
        elif not d.get('pdf_hash_matches'):reason='PDF integrity is not proven'
        elif d.get('operator_no_smtp_proof_sha256') and d['status'] in ('approved','validated'):
            kind='VALID_PENDING';reason='Requires explicit single V2 handover before admission; retained until then'
        else:
            kind='SMTP_UNCERTAIN';reason='Legacy row alone cannot prove that an earlier SMTP attempt did not happen'
        op={'operation_id':digest([task,attachment]),'task':task,'attachment_id':attachment,'category':kind,'reason':reason,
            'physical_messages':len(tasks),'unique_task_ids':sorted({t['task_id'] for t in tasks}),
            'document':semantic_document(d) if d else None,'route_role':d.get('current_route_role') if d else 'unknown'}
        operations.append(op)
        for i,t in enumerate(sorted(tasks,key=lambda t:(str(t.get('task_id')),str(t.get('delivery_tag')),t['envelope_sha256']))):
            rows.append({**t,'operation_id':op['operation_id'],'category':kind if i==0 or kind in ('UNKNOWN','SMTP_UNCERTAIN') else 'RETRY_DUPLICATE','operation_category':kind})
    summary={k:{'messages':sum(t['category']==k for t in rows),'documents':len({t['attachment_id'] for t in rows if t['category']==k and t['attachment_id']}) if k=='RETRY_DUPLICATE' else len({o['attachment_id'] for o in operations if o['category']==k})} for k in CATEGORIES}
    return {'format':'legacy-classification/v1','inventory_sha256':digest(snapshot),'observed_at':snapshot['observed_at'],
        'physical_messages':len(rows),'unique_task_ids':len(task_docs),'unique_documents':len(docs),'summary':summary,'operations':operations,'tasks':rows,
        'eta_groups':dict(collections.Counter((t.get('eta') or 'none')[:16] for t in rows)),
        'retry_groups':dict(collections.Counter(str(t.get('retries')) for t in rows))}

def prepare(snapshot,classified):
    if classified['inventory_sha256']!=digest(snapshot):raise ValueError('Classification does not match this inventory')
    actions={'OBSOLETE':'exclude_from_V2_replay; retain acceptance and escrow',
        'MISSING_PDF':'durable_missing_pdf_hold; preserve DB/hash/escrow; no auto replay on file recovery',
        'VALID_PENDING':'STOP until a separately verified single V2 handover exists; preserve operation',
        'SMTP_UNCERTAIN':'STOP; reconcile independent MTA evidence before any resend',
        'UNKNOWN':'STOP; resolve identity/outcome, preserve all transport data'}
    return {'format':'legacy-cutover-plan/v1','source_inventory_sha256':digest(snapshot),'base_rc':'d0881c6',
        'worker_identity':snapshot['worker_identity_after'],'producer_identity':snapshot['producer_identity'],
        'operations':[{**op,'action':actions[op['category']],'automatic_replay':False} for op in classified['operations']],
        'requires':'Owner GO on/after 2026-11-03, stopped legacy worker/beat, fresh inventory and two escrow copies; no task deletion'}

def verify(snapshot,plan,mode='planned',escrow=None,peer=None,local_host=None):
    errors=[]
    def need(value,reason):
        if not value:errors.append(reason)
    current=classify(snapshot);known={op['attachment_id']:op for op in plan['operations']}
    if mode!='planned':
        age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(snapshot['observed_at'])).total_seconds()
        need(0<=age<=60,'Snapshot must be refreshed within 60 seconds')
    need(snapshot.get('worker_view_stable') and snapshot.get('transport_unchanged'),'Inventory changed during observation')
    need(snapshot['worker_identity_before']==snapshot['worker_identity_after'],'Worker identity changed')
    for key in ('active','reserved'):need(snapshot['worker_after'].get(key)==[],'Active/reserved/unknown worker state: '+key)
    need(snapshot['worker_after'].get('scheduled') is not None,'Scheduled state is unknown')
    need(not snapshot.get('decode_errors') and not snapshot.get('missing_db_ids'),'Undecodable task or missing PostgreSQL document')
    need(not snapshot['redis'].get('hash_without_index') and not snapshot['redis'].get('index_without_hash'),'Redis unacked/index mismatch')
    need(not snapshot.get('unknown_redis_clients'),'Unknown Redis client/possible consumer')
    need(snapshot['redis']['unacked']==snapshot['redis']['unacked_index'],'Redis unacked count mismatch')
    need(len(snapshot['deliveries'])==sum(snapshot['redis']['lists'].values())+snapshot['redis']['unacked'],'Inventory does not cover every physical broker message')
    need(all(t.get('routing_key')=='celery' for t in snapshot['deliveries']),'Unexpected routing key')
    need(all(k=='celery' or k.startswith('celery\x06\x16') for k in snapshot['redis']['lists']),'Unknown legacy list key')
    if snapshot['worker_identity_after']['running']:
        signature=lambda t:(t.get('task_id'),t.get('task'),t.get('attachment_id'),t.get('eta'))
        need(collections.Counter(map(signature,snapshot['worker_after']['scheduled'] or []))==collections.Counter(signature(t) for t in snapshot['deliveries'] if t['location']=='unacked'),'Scheduled and unacked messages do not match')
    docs={d['attachment_id']:d for d in snapshot['documents']}
    for attachment,op in known.items():
        need(attachment in docs and semantic_document(docs[attachment])==op['document'],'Baseline document changed/disappeared: '+str(attachment))
        need(op['category'] in ('OBSOLETE','MISSING_PDF'),'Blocking operation '+str(attachment)+': '+op['category'])
    for op in current['operations']:
        need(op['attachment_id'] in known,'New/unplanned operation: '+str(op['attachment_id']))
        need(op['category'] not in ('UNKNOWN','SMTP_UNCERTAIN','VALID_PENDING'),'Blocking current operation: '+str(op['attachment_id'])+' '+op['category'])
    for key,old in [('worker_identity_after','worker_identity'),('producer_identity','producer_identity')]:
        need(all(snapshot[key].get(k)==plan[old].get(k) for k in ('id','image','started')),'Legacy container differs from plan: '+key)
    if mode=='drain':
        for op in plan['operations']:
            need(op['category']=='MISSING_PDF' or (op['category']=='OBSOLETE' and op['document']['status']=='sent'),
                'Natural legacy execution is unsafe for operation '+str(op['attachment_id']))
        need(all(t.get('retries')==3 for t in snapshot['deliveries']),'Drain requires final retry=3 only; wait read-only for a suitable barrier')
        for t in snapshot['deliveries']:
            try:
                delta=(datetime.datetime.fromisoformat(t['eta'])-datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                need(-10<=delta<=300,'Final retry ETA is outside the verified natural-drain window')
            except (ValueError,TypeError,KeyError):need(False,'Final retry ETA is unknown')
        need(snapshot['worker_identity_after']['running'],'Legacy worker is not running for natural drain')
    if mode in ('drained','fenced'):
        need(not snapshot['producer_identity']['running'] and snapshot['producer_identity']['exit_code']==0,'Legacy producer must be stopped cleanly')
        need(not snapshot['deliveries'] and snapshot['worker_after'].get('scheduled')==[],'Legacy transport is not fully drained; preserve and STOP')
    if mode=='drained':need(snapshot['worker_identity_after']['running'],'Pre-stop drain barrier must observe the legacy worker running')
    if mode=='fenced':
        need(not snapshot['worker_identity_after']['running'] and snapshot['worker_identity_after']['exit_code']==0,'Legacy worker must be stopped cleanly')
        need(not snapshot['producer_identity']['running'] and snapshot['producer_identity']['exit_code']==0,'Legacy producer must be stopped cleanly')
        need(not snapshot.get('legacy_consumers'),'A legacy consumer is still running')
        need(escrow is not None and snapshot.get('escrow_sha256')==hashlib.sha256(escrow or b'').hexdigest(),'Fresh escrow bytes/hash are missing')
        if escrow:
            import base64
            try:
                raw=json.loads(escrow)
                need(raw['format']=='redis-DUMP/v1' and {k:hashlib.sha256(base64.b64decode(v,validate=True)).hexdigest() for k,v in raw['keys'].items()}==snapshot['transport_key_sha256'],'Escrow does not cover the frozen transport')
                need(raw.get('handover_plan')==plan,'Escrow must retain the full operation plan, including PDF holds')
            except Exception:need(False,'Escrow format is invalid')
        need(peer is not None and peer.get('sha256')==snapshot.get('escrow_sha256') and peer.get('host')!=local_host,'Verified escrow copy on another host is missing')
    return {'format':'legacy-queue-proof/v1','verdict':'STOP' if errors else {'planned':'PLAN_READY','drain':'DRAIN_ALLOWED','drained':'DRAINED','fenced':'CUTOVER_READY'}[mode],
        'mode':mode,'reasons':errors,'observed_at':snapshot['observed_at'],'plan_sha256':digest(plan),'inventory_sha256':digest(snapshot),
        'worker_identity':snapshot['worker_identity_after'],'producer_identity':snapshot['producer_identity'],
        'transport_key_sha256':snapshot.get('transport_key_sha256',{}),'legacy_lists':snapshot['redis']['lists'],
        'legacy_unacked':snapshot['redis']['unacked'],'legacy_scheduled':len(snapshot['worker_after']['scheduled'] or []),
        'held_operations':len(plan['operations']),'automatic_replay_operations':0}
