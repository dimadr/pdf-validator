"""Read-only legacy inventory. Run on the Docker host, output outside production."""
import argparse,datetime,hashlib,inspect,json,pathlib,subprocess,sys

def inside(hostname,worker_running,extra_ids,export_escrow):
    import ast,base64,collections,datetime,hashlib,json,os,pathlib,re,time
    import psycopg2,redis
    from celery import Celery
    def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
    def attachment(args,kwargs):
        try:
            if isinstance(args,str):args=ast.literal_eval(args[:4096])
            if isinstance(kwargs,str):kwargs=ast.literal_eval(kwargs[:4096])
            value=(kwargs or {}).get('attachment_id') or (args[0] if args else None)
            return str(value) if value and re.fullmatch(r'[a-fA-F0-9-]{36}',str(value)) else None
        except (ValueError,SyntaxError,TypeError,AttributeError):return None
    def normalized(req,**extra):
        value=dict(task_id=req.get('id'),task=req.get('name') or req.get('task'),
            attachment_id=attachment(req.get('args'),req.get('kwargs')),eta=req.get('eta'),retries=req.get('retries'))
        value.update(extra);return value
    app=Celery('readonly-legacy-inventory',broker=os.environ['REDIS_URL'])
    app.conf.broker_transport_options={'global_keyprefix':''}
    def worker_view():
        out={}
        if not worker_running:return {'active':[],'reserved':[],'scheduled':[],'worker_stopped':True}
        view=app.control.inspect(destination=[hostname],timeout=4)
        for kind in ('active','reserved','scheduled'):
            result=getattr(view,kind)()
            out[kind]=None if result is None else [normalized(t.get('request') or t,
                eta=t.get('eta') or (t.get('request') or t).get('eta'),location=kind)
                for tasks in result.values() for t in tasks]
        stats=view.stats() or {}
        out['stats']={k:{'pid':v.get('pid'),'uptime':v.get('uptime'),'clock':v.get('clock'),
            'pool_processes':(v.get('pool') or {}).get('processes'),'broker_transport':(v.get('broker') or {}).get('transport')}
            for k,v in stats.items()}
        return out
    before=worker_view();r=redis.Redis.from_url(os.environ['REDIS_URL'],socket_timeout=5,socket_connect_timeout=5)
    keys=sorted(r.scan_iter());lists=[];other={}
    for key in keys:
        kind=r.type(key)
        if kind==b'list' and b'.reply.celery.pidbox' not in key:
            if r.llen(key)>10000:raise RuntimeError('STOP: bounded inventory limit exceeded')
            lists.append(key)
        elif kind not in (b'none',):other[key.decode(errors='replace')]=kind.decode()
    if r.hlen('unacked')>10000 or r.zcard('unacked_index')>10000:raise RuntimeError('STOP: unacked inventory limit exceeded')
    # MULTI contains reads only, giving one coherent broker slice.
    pipe=r.pipeline(transaction=True)
    for key in lists:pipe.lrange(key,0,-1)
    pipe.hgetall('unacked');pipe.zrange('unacked_index',0,-1,withscores=True);pipe.time()
    transport_keys=sorted(set(lists)|{b'unacked',b'unacked_index'})
    for key in transport_keys:pipe.dump(key)
    values=pipe.execute();n=len(lists);hashed=values[n];index=dict(values[n+1]);server_time=values[n+2]
    dumps={key.decode():value for key,value in zip(transport_keys,values[n+3:]) if value is not None}
    deliveries=[];decode_errors=[]
    def envelope(raw,location,tag=None,score=None):
        digest=hashlib.sha256(raw).hexdigest()
        try:
            data=json.loads(raw)
            if location=='unacked':data,exchange,routing_key=data
            else:exchange='';routing_key=(data.get('properties',{}).get('delivery_info') or {}).get('routing_key')
            head=data.get('headers') or {};body=json.loads(base64.b64decode(data['body'],validate=True))
            req=dict(head,args=body[0],kwargs=body[1]);req['name']=head.get('task')
            deliveries.append(normalized(req,location=location,delivery_tag=tag,unacked_score=score,
                envelope_sha256=digest,routing_key=routing_key,redelivered=bool((data.get('properties',{}).get('delivery_info') or {}).get('redelivered')),
                call_shape_known=(len(body[0])==1 and not body[1]) or (not body[0] and set(body[1])=={'attachment_id'}),
                root_id=head.get('root_id'),parent_id=head.get('parent_id')))
        except Exception:decode_errors.append({'location':location,'delivery_tag':tag,'sha256':digest})
    for key,items in zip(lists,values[:n]):
        for item in items:envelope(item,key.decode(errors='replace'))
    for tag,raw in sorted(hashed.items()):envelope(raw,'unacked',tag.decode(),index.get(tag))
    after=worker_view()
    result_meta={}
    for task_id in sorted({t['task_id'] for t in deliveries if t['task_id']}):
        raw=r.get('celery-task-meta-'+task_id)
        if not raw:continue
        try:
            meta=json.loads(raw);result=meta.get('result')
            result_meta[task_id]={'state':meta.get('status'),'date_done':meta.get('date_done'),
                'result_status':result.get('status') if isinstance(result,dict) else None,
                'missing_pdf_error':isinstance(result,dict) and 'PDF file not found' in str(result.get('message','')),
                'result_sha256':hashlib.sha256(raw).hexdigest()}
        except Exception:result_meta[task_id]={'state':'UNDECODABLE'}
    ids=sorted(set(extra_ids)|{t['attachment_id'] for t in deliveries+[v for k in ('active','reserved','scheduled') for v in (after[k] or [])] if t['attachment_id']})
    url=os.environ['DATABASE_URL'].replace('postgresql+psycopg2:','postgresql:')
    db=psycopg2.connect(url,connect_timeout=5);db.set_session(readonly=True,isolation_level='REPEATABLE READ')
    documents=[]
    with db.cursor() as q:
        q.execute("SET LOCAL statement_timeout='5s'")
        q.execute('SELECT version_num FROM alembic_version');schema=q.fetchone()[0]
        q.execute('SELECT a.id,a.status,a.reject_reason,a.file_path,a.file_sha256,a.sent_at,a.original_message_id,a.sent_to_email,a.updated_at,a.created_at,a.calculator_number,a.object_id,o.is_active,o.email FROM attachments a LEFT JOIN objects o ON o.id=a.object_id WHERE a.id=ANY(%s::uuid[]) ORDER BY a.id',(ids,))
        for row in q.fetchall():
            ident,status,reason,path,sha,sent_at,message_id,sent_to,updated,created,serial,object_id,active,obj_email=row
            # Role comparison happens here; actual email addresses never leave the container.
            accepted={v.strip().lower() for v in (sent_to or '').split(',') if v.strip()}
            admin={os.getenv(k,'').strip().lower() for k in ('ADMIN_EMAIL','NOTIFY_EMAIL')}-{''}
            expected=admin if status=='rejected' else {v.strip().lower() for v in (obj_email or '').split(',') if v.strip()}
            f=pathlib.Path(path or '/nonexistent');safe=bool(path) and f.resolve().is_relative_to(pathlib.Path('/app/uploads').resolve())
            exists=safe and f.is_file();actual=None
            if exists:
                if f.stat().st_size>25*1024*1024:raise RuntimeError('STOP: PDF exceeds bounded integrity check size')
                actual=hashlib.sha256(f.read_bytes()).hexdigest()
            documents.append({'attachment_id':str(ident),'status':status,'reject_reason':reason,
                'file_path_sha256':hashlib.sha256((path or '').encode()).hexdigest(),'file_path_safe':safe,'pdf_exists':exists,
                'pdf_hash_matches':exists and actual==sha,'content_sha256':sha,'sent_at':sent_at.isoformat() if sent_at else None,
                'message_id_present':bool(message_id),'accepted_recipient_count':len(accepted),
                'accepted_admin_count':len(accepted&admin),'accepted_nonadmin_count':len(accepted-admin),
                'all_expected_recorded':bool(expected) and expected.issubset(accepted),'expected_recipient_count':len(expected),
                'current_route_role':'admin_notification' if status=='rejected' else 'client' if expected else 'unresolved',
                'object_id':str(object_id) if object_id else None,'object_active':active,
                'serial_sha256':hashlib.sha256((serial or '').encode()).hexdigest(),'updated_at':updated.isoformat() if updated else None,
                'created_at':created.isoformat() if created else None})
    db.rollback();db.close()
    def signatures(view):return {k:None if view[k] is None else sorted(json.dumps(t,sort_keys=True) for t in view[k]) for k in ('active','reserved','scheduled')}
    check=r.pipeline(transaction=True)
    for key in transport_keys:check.dump(key)
    final_dumps={key.decode():value for key,value in zip(transport_keys,check.execute()) if value is not None}
    escrow={key:base64.b64encode(value).decode() for key,value in dumps.items()} if export_escrow else None
    return {'observed_at':stamp(),'worker_clock_utc':stamp(),'worker_epoch':time.time(),'redis_time':list(server_time),
        'worker_before':before,'worker_after':after,'worker_view_stable':signatures(before)==signatures(after),
        'redis':{'lists':{key.decode():len(v) for key,v in zip(lists,values[:n])},'unacked':len(hashed),'unacked_index':len(index),
            'hash_without_index':sorted(k.decode() for k in set(hashed)-set(index)),
            'index_without_hash':sorted(k.decode() for k in set(index)-set(hashed)),'other_key_types':other},
        'transport_unchanged':dumps==final_dumps,'transport_key_sha256':{key:hashlib.sha256(value).hexdigest() for key,value in dumps.items()},'escrow':escrow,
        'redis_client_ips':sorted({c['addr'].rsplit(':',1)[0] for c in r.client_list()}),
        'deliveries':deliveries,'result_meta':result_meta,'decode_errors':decode_errors,'schema':schema,'documents':documents,'missing_db_ids':sorted(set(ids)-{d['attachment_id'] for d in documents})}

def collect(worker='project1-worker',observer=None,extra_ids=(),export_escrow=False,producer='project1-celery-beat',node=None,network='project1_default'):
    def docker(*args):return subprocess.check_output(['docker',*args],text=True)
    original=json.loads(docker('inspect',worker))[0]
    observer=observer or (worker if original['State']['Running'] else 'project1-backend')
    script='import json\n'+inspect.getsource(inside)+'\nprint(json.dumps(inside('+','.join(map(repr,[node or 'celery@'+original['Config']['Hostname'],original['State']['Running'],list(extra_ids),export_escrow]))+')))'
    result=subprocess.run(['docker','exec','-i',observer,'python','-B','-'],input=script,text=True,capture_output=True,timeout=90)
    if result.returncode:raise RuntimeError('STOP: read-only container probe failed: '+result.stderr[-900:])
    data=json.loads(result.stdout)
    def identity(c):return {'id':c['Id'],'image':c['Image'],'running':c['State']['Running'],'started':c['State']['StartedAt'],'exit_code':c['State']['ExitCode']}
    data['worker_identity_before']=identity(original);data['worker_identity_after']=identity(json.loads(docker('inspect',worker))[0])
    data['host_clock_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    data['producer_identity']=identity(json.loads(docker('inspect',producer))[0])
    related=json.loads(docker('inspect',*docker('ps','-q').split()))
    data['legacy_consumers']=[c['Name'].lstrip('/') for c in related if network in c['NetworkSettings']['Networks'] and c['Image']==original['Image'] and 'worker' in ' '.join(c['Config']['Cmd'] or [])]
    data['known_app_ips']=[n['IPAddress'] for c in related for name,n in c['NetworkSettings']['Networks'].items() if name==network]
    data['unknown_redis_clients']=sorted(set(data['redis_client_ips'])-set(data['known_app_ips']))
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--worker',default='project1-worker');p.add_argument('--observer');p.add_argument('--include-ids-from',type=pathlib.Path);p.add_argument('--escrow',type=pathlib.Path);p.add_argument('--plan',type=pathlib.Path);a=p.parse_args()
    target=a.output.resolve()
    if target==pathlib.Path('/root/project1') or pathlib.Path('/root/project1') in target.parents:raise SystemExit('STOP: output must be outside production')
    extra=[] if not a.include_ids_from else [d['attachment_id'] for d in json.loads(a.include_ids_from.read_text())['documents']]
    plan=json.loads(a.plan.read_text()) if a.plan else None
    if plan:extra.extend(op['attachment_id'] for op in plan['operations'])
    if a.escrow and pathlib.Path('/root/project1') in a.escrow.resolve().parents:raise SystemExit('STOP: escrow must be outside production')
    data=collect(a.worker,a.observer,extra,bool(a.escrow));escrow=data.pop('escrow')
    if a.escrow:
        raw=(json.dumps({'format':'redis-DUMP/v1','keys':escrow,'handover_plan':plan},sort_keys=True)+'\n').encode()
        with a.escrow.open('xb') as stream:stream.write(raw)
        a.escrow.chmod(0o600);data['escrow_sha256']=hashlib.sha256(raw).hexdigest()
    target.parent.mkdir(exist_ok=True,parents=True);target.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({'snapshot':str(target),'observed_at':data['observed_at'],'stable':data['worker_view_stable'],
        'worker_counts':{k:None if data['worker_after'][k] is None else len(data['worker_after'][k]) for k in ('active','reserved','scheduled')},
        'redis_counts':{k:v for k,v in data['redis'].items() if k!='other_key_types'},'unique_documents':len(data['documents'])}))
if __name__=='__main__':main()
