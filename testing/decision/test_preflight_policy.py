"""Targeted admission/rollback policy tests; no Docker, network, DB or mail effects."""
import copy,datetime,importlib.util,pathlib
import pytest
ROOT=pathlib.Path(__file__).resolve().parents[2]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
pf=module('decision_preflight',ROOT/'deploy/preflight.py')
guard=module('decision_guard',ROOT/'deploy/deployment_guard.py')
canary=module('decision_canary',ROOT/'deploy/canary_once.py')
def facts():
    instant='2026-11-03T10:00:00+00:00'
    return {'images':{k:True for k in pf.IMAGES},'containers':{
        'project1-postgres':{'id':'pg','running':True,'mounts':[{'Type':'volume','Name':pf.PG_VOLUME,'Destination':'/var/lib/postgresql/data','RW':True}]},
        'project1-redis':{'id':'redis','running':True,'mounts':[{'Type':'volume','Name':pf.REDIS_VOLUME,'Destination':'/data','RW':True}]},
        'project1-worker':{'id':'old-worker','running':True,'started':instant,'exit_code':0},
        'project1-celery-beat':{'id':'old-beat','running':True,'started':instant,'exit_code':0}},
        'uploads_present':True,'app_mounts':[], 'schema':'0004','environment':{k:True for k in (
        'DATABASE_URL','REDIS_URL','SECRET_KEY','SMTP_SERVER','SMTP_USER','SMTP_PASSWORD','IMAP_SERVER','IMAP_USER','IMAP_PASSWORD','smtp_from_effective_present','smtp_port_valid')},
        'env_file_matches_running_app':{k:True for k in ('DATABASE_URL','REDIS_URL','SECRET_KEY','SMTP_SERVER','SMTP_USER','SMTP_PASSWORD','IMAP_SERVER','IMAP_USER','IMAP_PASSWORD')},
        'redis_ping':True,'senders':['project1-worker'],'producers':['project1-celery-beat'],
        'inspection':{'project1-worker':{'active':0,'reserved':0,'scheduled':0}},
        'redis':{'nonempty_lists':{},'unacked':0},'legacy_unfinished':0,
        'imap':{'uidvalidity':77,'uidnext':6,'last_uid':5,'safe_bootstrap_candidate':5,'verified_legacy_anchor':True,'unclassified_uids':[]},
        'v2':{'revisions':0,'smtp_attempts':0,'in_flight':0,'uncertain':0,'pending':0,'unpublished':0,'ai_enabled':False,'checkpoint':None},'observed_at':instant}
def stopped():
    old=facts();barrier={'verdict':'PASS','observed_at':old['observed_at'],'facts':copy.deepcopy(old)}
    current=copy.deepcopy(old);current.update(schema='0007',v2_tables_complete=True,senders=[],producers=[],inspection={})
    for name in ('project1-worker','project1-celery-beat'):current['containers'][name]['running']=False
    return current,barrier
def test_valid_prepare_and_legacy_handback_before_v2_operations():
    assert not pf.evaluate(facts(),'prepare')
    current,barrier=stopped();assert not pf.evaluate(current,'legacy-return',barrier)
    current['schema']='0004';assert not pf.evaluate(current,'legacy-return',barrier)
def test_safe_v2_resume_after_durable_acceptance():
    current,barrier=stopped();current['v2'].update(revisions=1,smtp_attempts=1,checkpoint={'uidvalidity':77,'last_uid':5})
    assert not pf.evaluate(current,'v2-resume',barrier)

def test_mailbox_schema_is_supported_without_weakening_completeness_guard():
    current,barrier=stopped();current['schema']='0009'
    current['v2'].update(revisions=1,smtp_attempts=1,checkpoint={'uidvalidity':77,'last_uid':5})
    assert not pf.evaluate(current,'v2-resume',barrier)
    current['v2_tables_complete']=False
    assert pf.evaluate(current,'v2-resume',barrier)
@pytest.mark.parametrize('mutation',[
    lambda f:f['images'].update(worker=False),
    lambda f:f['containers']['project1-postgres']['mounts'][0].update(Name='empty-new-volume'),
    lambda f:f['containers']['project1-redis']['mounts'][0].update(RW=False),
    lambda f:f.update(uploads_present=False),
    lambda f:f.update(schema='0003'),
    lambda f:f['environment'].update(SMTP_PASSWORD=False),
    lambda f:f['env_file_matches_running_app'].update(DATABASE_URL=False),
    lambda f:f['inspection']['project1-worker'].update(active=1),
    lambda f:f['inspection']['project1-worker'].update(scheduled=None),
    lambda f:f['redis'].update(unacked=1),
    lambda f:f['redis'].update(nonempty_lists={'unknown-task-queue':1}),
    lambda f:f.update(senders=['project1-worker','project1-v2-worker']),
    lambda f:f.update(legacy_unfinished=1),
    lambda f:f['imap'].update(unclassified_uids=[5]),
    lambda f:f['imap'].update(verified_legacy_anchor=False),
    lambda f:f['v2'].update(ai_enabled=True),
])
def test_every_mandatory_violation_stops(mutation):
    f=facts();mutation(f);assert pf.evaluate(f,'prepare')
@pytest.mark.parametrize('key,value',[('revisions',1),('smtp_attempts',1),('unpublished',1)])
def test_legacy_handback_blocks_v2_work_even_before_first_ack(key,value):
    current,barrier=stopped();current['v2'][key]=value
    assert pf.evaluate(current,'legacy-return',barrier)
def test_unknown_smtp_blocks_unconditional_automatic_resume():
    current,barrier=stopped();current['v2']['uncertain']=1
    assert pf.evaluate(current,'v2-resume',barrier)
def test_changed_uid_or_stale_drain_or_unclean_shutdown_stops():
    current,barrier=stopped();current['imap']['uidvalidity']=78
    assert pf.evaluate(current,'cutover-ready',barrier)
    current,barrier=stopped();barrier['observed_at']='2026-11-03T09:00:00+00:00'
    assert pf.evaluate(current,'cutover-ready',barrier)
    current,barrier=stopped();current['containers']['project1-worker']['exit_code']=137
    assert pf.evaluate(current,'cutover-ready',barrier)
@pytest.mark.parametrize('stamp,approval,allowed',[
    ('2026-11-01T20:59:59+00:00',guard.APPROVAL,False),
    ('2026-11-01T21:00:00+00:00','',False),
    ('2026-11-01T21:00:00+00:00',guard.APPROVAL,True),
    ('2026-11-02T08:00:00+00:00',guard.APPROVAL,True),
    ('2026-11-02T08:00:00+00:00','GO',False),
    ('2026-10-30T12:00:00+00:00',guard.APPROVAL,False),
])
def test_hard_date_and_separate_owner_gate(stamp,approval,allowed):
    assert guard.permitted(approval,datetime.datetime.fromisoformat(stamp))[0] is allowed

@pytest.mark.parametrize('key',['revisions','smtp_attempts','unpublished'])
def test_first_cutover_requires_fresh_ledger(key):
    current,barrier=stopped();current['v2'][key]=1
    assert pf.evaluate(current,'cutover-ready',barrier)

def test_manual_canary_resume_does_not_require_a_fabricated_imap_checkpoint():
    current,barrier=stopped();current['v2'].update(revisions=1,smtp_attempts=1,imap_revisions=0)
    current['observed_at']='2026-11-03T11:00:00+00:00'
    assert not pf.evaluate(current,'v2-resume',barrier)
    current['v2']['imap_revisions']=1
    assert pf.evaluate(current,'v2-resume',barrier)

@pytest.mark.parametrize('change',[{'producers':['project1-v2-celery-beat']},{'senders':['shadow-legacy-worker']}])
def test_recovery_gate_rejects_producers_and_unknown_sender(change):
    current,barrier=stopped();current.update(change)
    assert pf.evaluate(current,'v2-resume',barrier)

@pytest.mark.parametrize('method',['prepare','wait_for_acceptance'])
def test_canary_publish_and_prepare_both_enforce_owner_gate(monkeypatch,method):
    monkeypatch.setenv('DATABASE_URL','postgresql://production/project1')
    monkeypatch.delenv('PROJECT1_PRODUCTION_APPROVAL',raising=False)
    monkeypatch.delenv('PROJECT1_TEST_SCOPE',raising=False)
    with pytest.raises(RuntimeError,match='STOP:'):
        getattr(canary,method)({})
