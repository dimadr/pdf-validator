"""Isolated-only old-image worker; scale retry timers, never change task logic."""
import os,json,pathlib,sys
assert os.getenv('PROJECT1_TEST_SCOPE')=='production-decision-copy'
assert '@postgres:5432/project1_test_decision' in os.environ['DATABASE_URL']
sys.path.insert(0,os.getcwd())
from celery_app import celery_app
from email_sender import send_pdf_attachment
retry=send_pdf_attachment.retry
def timed_retry(*args,**kwargs):
    row={'task_id':send_pdf_attachment.request.id,'retries':send_pdf_attachment.request.retries,'original_countdown':kwargs.get('countdown')}
    with pathlib.Path('/data/retry-calls.jsonl').open('a') as stream:stream.write(json.dumps(row)+'\n')
    kwargs['countdown']=20 if row['retries']==2 else .2
    return retry(*args,**kwargs)
send_pdf_attachment.retry=timed_retry
celery_app.worker_main(['worker','--hostname=legacy@decision','--concurrency=1','--prefetch-multiplier=1','--loglevel=INFO','--without-mingle','--without-gossip'])
