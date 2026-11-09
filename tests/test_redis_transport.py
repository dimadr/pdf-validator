"""Acceptance uses actual Redis7 and Linux prefork Celery processes; otherwise explicitly skipped."""
import os,sys,uuid,time,subprocess,signal,json
from pathlib import Path
from datetime import timedelta
from contextlib import contextmanager
import pytest,redis
from celery import Celery
from sqlalchemy import select
from common.models import RecipientDelivery,OutboxEvent
from common.delivery import dispatch_outbox,reconcile,now
from test_delivery_core import planned
from test_local_protocols import running,SMTPHandler

pytestmark=pytest.mark.skipif(not sys.platform.startswith('linux') or not os.getenv('TEST_REDIS_URL'),
    reason='NOT RUN: real Redis7/Linux prefork acceptance requires isolated Linux test stack')

def wait(predicate,seconds=40):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.2)
    raise AssertionError('Timed out waiting for isolated worker transition')

@contextmanager
def runtime(monkeypatch,tmp_path,sink):
    url=os.environ['TEST_REDIS_URL'];from urllib.parse import urlparse
    assert urlparse(url).hostname in ('redis','127.0.0.1','localhost')
    client=redis.Redis.from_url(url);assert client.info()['redis_version'].startswith('7.')
    suffix=uuid.uuid4().hex;queue='rc_test_'+suffix;prefix=queue+':'
    producer=Celery('acceptance',broker=url,backend=url)
    producer.conf.update(broker_transport_options={'global_keyprefix':prefix,'visibility_timeout':5},
        result_backend_transport_options={'global_keyprefix':prefix},task_default_queue=queue)
    environment=dict(os.environ,DATABASE_URL=os.environ['TEST_DATABASE_URL'],REDIS_URL=url,CELERY_QUEUE=queue,
        CELERY_REDIS_KEY_PREFIX=prefix,CELERY_VISIBILITY_TIMEOUT='5',PIPELINE_V2_ENABLED='true',SMTP_TEST_MODE='true',
        SMTP_SERVER='127.0.0.1',SMTP_PORT=str(sink.server_address[1]),SMTP_FROM='processor@example.invalid',SMTP_USER='',
        PYTHONPATH=str(Path(__file__).resolve().parents[1]/'worker')+os.pathsep+str(Path(__file__).resolve().parents[1]))
    processes=[];handles=[]
    def start():
        name='rc_'+str(len(processes))+'_'+suffix+'@localhost'
        logroot=Path(os.getenv('TEST_ARTIFACT_DIR',str(tmp_path)));logroot.mkdir(parents=True,exist_ok=True)
        log=(logroot/(name.split('@')[0]+'.log')).open('wb');handles.append(log)
        process=subprocess.Popen([sys.executable,'-m','celery','-A','celery_app','worker','--pool=prefork','--concurrency=1',
            '--queues='+queue,'--hostname='+name,'--loglevel=WARNING','--without-mingle','--without-gossip','--include=testing.celery_fault_tasks'],
            cwd=Path(__file__).resolve().parents[1],env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        processes.append(process)
        wait(lambda: producer.control.ping(destination=[name],timeout=.5),40)
        return process
    def kill(process):
        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
    try:yield producer,client,queue,start,kill
    finally:
        for process in processes:kill(process)
        producer.close()
        for handle in handles:handle.close()

def test_two_real_celery_workers_read_live_ai_switch(session_factory,monkeypatch,tmp_path):
    from common.ai_settings import set_enabled
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-test-key')
    with running(SMTPHandler) as sink,runtime(monkeypatch,tmp_path,sink) as (app,client,queue,start,kill):
        start();start()
        for value in (False,True,False):
            with session_factory() as db:
                with db.begin():set_enabled(db,value,'synthetic-admin')
            tasks=[app.send_task('testing.ai_status',queue=queue) for _ in range(2)]
            results=[task.get(timeout=20) for task in tasks]
            assert len({r['pid'] for r in results})==2
            assert all(r['effective_ai']==value for r in results)


def test_real_redis_two_workers_and_duplicate_outbox(session_factory,seed,monkeypatch,tmp_path):
    delivery=planned(session_factory,seed())[0]
    with running(SMTPHandler) as sink,runtime(monkeypatch,tmp_path,sink) as (app,client,queue,start,kill):
        start();start();results=[]
        def publish(topic,payload):results.append(app.send_task(topic,kwargs=payload,queue=queue))
        def crash(event):raise RuntimeError('Crash after actual Redis publication before PG commit')
        assert dispatch_outbox(session_factory,publish,after_publish=crash)==0
        assert dispatch_outbox(session_factory,publish)==1
        for result in results:result.get(timeout=30)
        assert len(sink.received)==1
        with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='accepted'

@pytest.mark.parametrize('phase',['before_claim','after_claim'])
def test_actual_worker_kill_and_redis_redelivery(session_factory,seed,monkeypatch,tmp_path,phase):
    delivery=planned(session_factory,seed())[0]
    with running(SMTPHandler) as sink,runtime(monkeypatch,tmp_path,sink) as (app,client,queue,start,kill):
        worker=start();key='rc_test_'+uuid.uuid4().hex
        result=app.send_task('test.delivery_fault',kwargs={'delivery_id':str(delivery),'key':key,'phase':phase},queue=queue)
        wait(lambda:client.get(key))
        kill(worker)
        # Kombu scans on startup, then every tenth 10-second restore timer call.
        # Starting before visibility expiry defers the next real scan to ~100s.
        # Verify the actual unacked record and let its visibility expire before replacement boot.
        reserved=[]
        for tag,payload in client.hgetall(queue+':unacked').items():
            message,exchange,routing=json.loads(payload)
            if message.get('headers',{}).get('id')==result.id:
                reserved.append(client.zscore(queue+':unacked_index',tag))
        assert len(reserved)==1 and reserved[0] is not None
        wait(lambda:time.time()>reserved[0]+6,15)
        start()
        result.get(timeout=50)
        if phase=='before_claim':
            assert len(sink.received)==1
            with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='accepted'
        else:
            assert not sink.received
            with session_factory() as db:
                with db.begin():db.get(RecipientDelivery,delivery).lease_until=now()-timedelta(seconds=1)
            assert reconcile(session_factory)['uncertain']==1
            with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='uncertain'
