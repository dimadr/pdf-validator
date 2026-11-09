"""Targeted invariants for retained legacy operations and producer drain."""
import base64,copy,datetime,hashlib,importlib.util,json,pathlib,sys
import pytest
ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/legacy_queue'))
from queue_policy import classify,prepare,verify
from test_preflight_policy import facts,pf
def inventory():
    eta=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=240)).isoformat()
    tasks=[{'task_id':'task-one','task':'email_sender.send_pdf_attachment','attachment_id':'doc-one','eta':eta,
        'retries':3,'location':'unacked','delivery_tag':str(i),'envelope_sha256':str(i),'call_shape_known':True,'routing_key':'celery'} for i in range(2)]
    doc={'attachment_id':'doc-one','status':'rejected','reject_reason':'send_error','pdf_exists':False,'pdf_hash_matches':False,'file_path_safe':True,
        'content_sha256':'pdf-hash','sent_at':None,'message_id_present':False,'accepted_recipient_count':0,'all_expected_recorded':False,
        'current_route_role':'admin_notification','object_id':'object-one','object_active':True}
    identity={'id':'worker','image':'old','started':'same','running':True,'exit_code':0}
    return {'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'worker_view_stable':True,'transport_unchanged':True,
        'worker_before':{'active':[],'reserved':[],'scheduled':copy.deepcopy(tasks)},'worker_after':{'active':[],'reserved':[],'scheduled':copy.deepcopy(tasks)},
        'worker_identity_before':copy.deepcopy(identity),'worker_identity_after':identity,
        'producer_identity':{**identity,'id':'producer'},'legacy_consumers':['worker'],
        'redis':{'lists':{},'unacked':2,'unacked_index':2,'hash_without_index':[],'index_without_hash':[]},
        'transport_key_sha256':{},'deliveries':tasks,'documents':[doc],'decode_errors':[],'missing_db_ids':[],'unknown_redis_clients':[]}
def planned(s):return prepare(s,classify(s))
def fenced():
    s=inventory();p=planned(s)
    for k in ('worker_identity_before','worker_identity_after','producer_identity'):s[k]['running']=False
    s['worker_before']['scheduled']=[];s['worker_after']['scheduled']=[];s['deliveries']=[];s['legacy_consumers']=[]
    s['redis'].update(unacked=0,unacked_index=0)
    raw=json.dumps({'format':'redis-DUMP/v1','keys':{},'handover_plan':p}).encode();s['escrow_sha256']=hashlib.sha256(raw).hexdigest()
    return s,p,raw,{'sha256':s['escrow_sha256'],'host':'peer'}
def test_counts_are_physical_messages_not_scheduled_plus_unacked():
    c=classify(inventory());assert c['physical_messages']==2 and c['unique_task_ids']==1
    assert c['summary']['MISSING_PDF']['messages']==1 and c['summary']['RETRY_DUPLICATE']['messages']==1
def test_missing_pdf_is_a_retained_hold_and_not_permission_to_delete_or_replay():
    s=inventory();p=planned(s);assert not p['operations'][0]['automatic_replay']
    assert 'hold' in p['operations'][0]['action'];assert verify(s,p,'drain')['verdict']=='DRAIN_ALLOWED'
@pytest.mark.parametrize('kind',['UNKNOWN','SMTP_UNCERTAIN','VALID_PENDING'])
def test_required_or_ambiguous_operation_is_preserved_and_blocks(kind):
    s=inventory();d=s['documents'][0]
    if kind=='UNKNOWN':s['deliveries'][0]['call_shape_known']=False
    elif kind=='SMTP_UNCERTAIN':d.update(sent_at='recorded',accepted_recipient_count=1)
    else:d.update(pdf_exists=True,pdf_hash_matches=True,status='validated',operator_no_smtp_proof_sha256='independent-proof')
    p=planned(s);assert p['operations'][0]['category']==kind
    assert p['operations'][0]['attachment_id']=='doc-one' and verify(s,p)['verdict']=='STOP'
def test_admin_acceptance_is_obsolete_notification_not_proof_of_client_delivery():
    s=inventory();s['documents'][0].update(sent_at='recorded',message_id_present=True,accepted_recipient_count=2,all_expected_recorded=True)
    p=planned(s);assert p['operations'][0]['category']=='OBSOLETE' and p['operations'][0]['route_role']=='admin_notification'
    assert verify(s,p,'drain')['verdict']=='STOP'  # legacy rejected sender would send admin mail again
@pytest.mark.parametrize('mutation',[
    lambda s:s['worker_after'].update(active=[{}]),lambda s:s['worker_after'].update(reserved=None),
    lambda s:s['redis'].update(index_without_hash=['orphan']),lambda s:s.update(unknown_redis_clients=['unexplained']),
    lambda s:s.update(transport_unchanged=False),lambda s:s['documents'][0].update(pdf_exists=True),
    lambda s:s['documents'][0].update(file_path_safe=False),
    lambda s:s['deliveries'][0].update(retries=2),lambda s:s.update(observed_at='2026-01-01T00:00:00+00:00')])
def test_unsafe_drain_stops_without_mutating_input(mutation):
    s=inventory();p=planned(s);mutation(s);before=copy.deepcopy(s)
    assert verify(s,p,'drain')['verdict']=='STOP' and s==before
def test_fenced_ready_retains_documents_when_queue_is_empty():
    s,p,raw,peer=fenced();r=verify(s,p,'fenced',raw,peer,'local')
    assert r['verdict']=='CUTOVER_READY' and r['held_operations']==1 and r['automatic_replay_operations']==0
    c=classify(s);assert c['physical_messages']==0 and c['summary']['MISSING_PDF']['documents']==1
    assert len(prepare(s,c)['operations'])==1
@pytest.mark.parametrize('fault',['worker','producer','escrow','peer','plan'])
def test_fenced_gate_requires_stops_and_two_matching_complete_copies(fault):
    s,p,raw,peer=fenced()
    if fault=='worker':s['worker_identity_after']['running']=True
    if fault=='producer':s['producer_identity']['running']=True
    if fault=='escrow':raw=b'changed'
    if fault=='peer':peer['host']='local'
    if fault=='plan':p['operations']=[]
    assert verify(s,p,'fenced',raw,peer,'local')['verdict']=='STOP'
def test_preflight_accepts_stopped_producer_only_with_fresh_drain_proof_and_empty_transport():
    f=facts();f['producers']=[];f['containers']['project1-celery-beat']['running']=False
    proof={'format':'legacy-queue-proof/v1','verdict':'DRAINED','observed_at':f['observed_at'],
        'worker_identity':f['containers']['project1-worker'],'producer_identity':f['containers']['project1-celery-beat']}
    assert not pf.evaluate(f,'prepare',legacy_drain_proof=proof)
    f['redis']['unacked']=1;assert pf.evaluate(f,'prepare',legacy_drain_proof=proof)
    f['redis']['unacked']=0;proof['verdict']='PLAN_READY';assert pf.evaluate(f,'prepare',legacy_drain_proof=proof)
