"""Real PG concurrency and pipeline tests. External provider is always a stub."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import uuid
import pytest
from sqlalchemy import select,func,text
from common.models import AIControl,AIRequest,AISettingAudit,ProcessingRevision,RecipientDelivery,utcnow
from common.ai_settings import set_enabled,status
from common.ai_fallback import run_fallback,needs_ai,ProviderError,Extraction,RouterAI
from common.pipeline import validate_revision

REVIEW={'state':'review_required','identity_state':'missing','calculator_number':None,
        'serial_candidates':[],'reason':'identity_missing','issues':[{'severity':'review_required','code':'identity_missing'}]}
EXTRACT={'serial_candidates':['005545-1'],'instrument_model':'ВКТ-7','billing_period':['2026-09-01','2026-09-01'],
    'series':[{'headers':['Дата','Q'],'required_columns':[1],
        'rows':[{'cells':['01.09.2026','10'],'evidence':{'page':1,'quote':'01.09.2026 10'}}],
        'evidence':{'page':1,'quote':'Дата Q'}}],
    'evidence':[{'page':1,'quote':'ВКТ-7 Заводской номер: 005545-1'}],
    'ambiguities':[],'parser_engine':'cloudflare-ai','prompt_version':'extract-evidence/1'}

class Stub:
    def __init__(self,error=None,hook=None):self.calls=0;self.error=error;self.hook=hook
    def extract(self,*args):
        self.calls+=1
        if self.hook:self.hook()
        if self.error:raise self.error
        return copy.deepcopy(EXTRACT),{'total_tokens':20,'cost':'unknown'}

def enable(factory,monkeypatch):
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-test-key')
    monkeypatch.delenv('AI_FORCE_DISABLED',raising=False)
    with factory() as db:
        with db.begin():set_enabled(db,True,'synthetic-admin')

def toggle(factory,value):
    with factory() as db:
        with db.begin():return set_enabled(db,value,'synthetic-admin')

def prepare(factory,seed,monkeypatch):
    enable(factory,monkeypatch);return seed('received')

def test_default_off_empty_key_and_force_disable(session_factory,seed,monkeypatch):
    item=seed('received');provider=Stub()
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-test-key')
    assert run_fallback(session_factory,item['revision'],REVIEW,{},provider)['ai']['state']=='disabled'
    toggle(session_factory,True)
    # A delayed old document cannot become AI-eligible simply by turning ON.
    assert run_fallback(session_factory,item['revision'],REVIEW,{},provider)['ai']['state']=='disabled'
    item=seed('received')
    monkeypatch.setenv('ROUTERAI_API_KEY','')
    assert run_fallback(session_factory,item['revision'],REVIEW,{},provider)['ai']['state']=='disabled'
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-test-key');monkeypatch.setenv('AI_FORCE_DISABLED','true')
    assert run_fallback(session_factory,item['revision'],REVIEW,{},provider)['ai']['state']=='disabled'
    assert provider.calls==0

def test_live_toggle_epoch_persistence_and_audit(session_factory,monkeypatch):
    enable(session_factory,monkeypatch)
    with session_factory() as db:assert status(db)['effective_ai']
    toggle(session_factory,False)
    with session_factory() as db:
        assert not status(db)['effective_ai']
        rows=db.scalars(select(AISettingAudit).order_by(AISettingAudit.created_at)).all()
        assert [(r.previous,r.enabled) for r in rows]==[(False,True),(True,False)]
        assert all(r.actor=='synthetic-admin' for r in rows)

@pytest.mark.parametrize('code,unknown',[('http_400',False),('http_401',False),('http_403',False),('http_429',False),('http_500',False),('timeout',True)])
def test_provider_errors_bounded_and_never_validate(session_factory,seed,monkeypatch,code,unknown):
    item=prepare(session_factory,seed,monkeypatch);provider=Stub(ProviderError(code,unknown))
    result=run_fallback(session_factory,item['revision'],REVIEW,{},provider)
    assert result['state']=='review_required'
    assert provider.calls==(2 if code=='http_500' else 1)
    run_fallback(session_factory,item['revision'],REVIEW,{},provider)
    # Completed error reservations also prevent duplicate paid effects.
    assert provider.calls==(2 if code=='http_500' else 1)

def test_disable_during_first_call_blocks_second_and_client_delivery(session_factory,seed,monkeypatch):
    item=prepare(session_factory,seed,monkeypatch)
    provider=Stub(hook=lambda:toggle(session_factory,False))
    result=run_fallback(session_factory,item['revision'],REVIEW,{},provider)
    assert result['state']=='review_required' and provider.calls==1
    assert result['ai']['state']=='failed'

def test_cache_hit_duplicate_tasks_and_daily_limit_atomic(session_factory,seed,monkeypatch):
    item=prepare(session_factory,seed,monkeypatch);provider=Stub(ProviderError('timeout',True))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _:run_fallback(session_factory,item['revision'],REVIEW,{},provider),range(12)))
    assert provider.calls==1
    items=[seed('received') for _ in range(24)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i:run_fallback(session_factory,i['revision'],REVIEW,{},provider),items))
    with session_factory() as db:assert db.scalar(select(func.count()).select_from(AIRequest))==20
    assert provider.calls==20

def test_verified_result_revalidated_and_cache_avoids_repeat(session_factory,seed,monkeypatch):
    item=prepare(session_factory,seed,monkeypatch);provider=Stub()
    verified={'state':'validated','identity_state':'unique','calculator_number':item['serial'],'issues':[],'reason':'validated'}
    monkeypatch.setattr('common.ai_fallback.verify',lambda *args:verified)
    first=run_fallback(session_factory,item['revision'],REVIEW,{},provider)
    second=run_fallback(session_factory,item['revision'],REVIEW,{},provider)
    assert first['state']=='validated' and second['state']=='validated'
    assert provider.calls==1 and second['ai']['attempts'][0]['cache_hit']
    assert first['deterministic_result']==REVIEW

def test_confirmed_reject_and_ordinary_pdf_make_no_calls(session_factory,seed,monkeypatch):
    item=prepare(session_factory,seed,monkeypatch);provider=Stub()
    for result in ({**REVIEW,'state':'validated'},{**REVIEW,'state':'rejected'},
        {**REVIEW,'issues':[{'code':'missing_daily_dates','severity':'rejected'}]}):
        assert run_fallback(session_factory,item['revision'],result,{},provider)==result
    assert provider.calls==0

def test_real_pipeline_off_deterministic_delivery_and_review(session_factory,seed,monkeypatch):
    monkeypatch.setattr(RouterAI,'extract',lambda *args:pytest.fail('Network call while OFF'))
    item=seed('received')
    result=validate_revision(session_factory,item['revision'],analyzer=lambda *a,**kw:{'state':'validated',
        'identity_state':'unique','calculator_number':item['serial'],'reason':'validated'})
    assert result['state']=='validated'
    with session_factory() as db:
        assert len(db.scalars(select(RecipientDelivery).where(RecipientDelivery.role=='client')).all())==2
    other=seed('received')
    assert validate_revision(session_factory,other['revision'],analyzer=lambda *a,**kw:REVIEW)['state']=='review_required'

def test_no_live_pdf_transmission_without_separate_consent(monkeypatch):
    monkeypatch.delenv('AI_EXTERNAL_DOCUMENTS_APPROVED',raising=False)
    with pytest.raises(ProviderError,match='external_consent_required'):
        RouterAI().extract(b'%PDF synthetic','openai/gpt-4o-mini','cloudflare-ai')

@pytest.mark.parametrize('change',[{'recipient':'wrong@example.invalid'}, {'serial_candidates':[5545]}, {'ambiguities':'no'}])
def test_strict_json_refuses_extra_fields_numbers_and_free_text(change):
    with pytest.raises(ValueError):Extraction.model_validate({**EXTRACT,**change})


def test_real_synthetic_pdf_evidence_and_revalidator(tmp_path):
    from testing.synthetic_pdf import report,extraction
    from common.pdf_validation import analyze_pdf
    from common.ai_fallback import verify
    path=tmp_path/'complex.pdf';path.write_bytes(report('005545-2',grid=False))
    original=analyze_pdf(path)
    assert original['state']=='review_required'
    result=verify(path,extraction(),original,{})
    assert result['state']=='validated' and result['calculator_number']=='005545-2'
    wrong=extraction();wrong['series'][0]['rows'][0]['cells'][-1]='9999'
    with pytest.raises(ValueError,match='row_evidence_mismatch'):verify(path,wrong,original,{})
    wrong=extraction();wrong['serial_candidates']=['2026']
    with pytest.raises(ValueError,match='serial_evidence_missing'):verify(path,wrong,original,{})


def test_grid_synthetic_report_is_independent_of_ai(tmp_path):
    from testing.synthetic_pdf import report
    from common.pdf_validation import analyze_pdf
    path=tmp_path/'normal.pdf';path.write_bytes(report())
    assert analyze_pdf(path)['state']=='validated'
    path.write_bytes(report(missing=True))
    assert analyze_pdf(path)['state']=='rejected'


def test_routerai_wire_contract_private_file_and_model_listing(monkeypatch):
    import httpx
    monkeypatch.delenv('AI_PROVIDER_MODE',raising=False)
    monkeypatch.setenv('AI_EXTERNAL_DOCUMENTS_APPROVED','true')
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-private-key')
    observed=[];original_client=httpx.Client
    def handler(request):
        observed.append(request)
        assert request.url.host=='routerai.ru'
        if request.method=='GET':return httpx.Response(200,json={'data':[{'id':'openai/gpt-4o-mini'}]})
        import json
        payload=json.loads(request.content)
        assert payload['messages'][0]['content'][1]['file']['file_data'].startswith('data:application/pdf;base64,')
        assert payload['plugins']==[{'id':'file-parser','pdf':{'engine':'cloudflare-ai'}}]
        assert 'client' not in payload and 'recipient' not in payload
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(EXTRACT)}}],'usage':{'total_tokens':30}})
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:original_client(transport=httpx.MockTransport(handler),**kwargs))
    result,usage=RouterAI().extract(b'%PDF synthetic','openai/gpt-4o-mini','cloudflare-ai')
    assert result==EXTRACT and usage['total_tokens']==30
    assert len(observed)==2
    with pytest.raises(ProviderError,match='model_unavailable'):
        RouterAI().extract(b'%PDF synthetic','missing/model','cloudflare-ai')
    assert len(observed)==3  # No completion for an unavailable model.


def test_193_documents_through_real_pipeline_off_zero_ai_calls(session_factory,monkeypatch):
    import os,json,hashlib
    from pathlib import Path
    from common.models import EmailSource,IncomingMessage,Attachment,Object,DeliveryIntent
    from common.pdf_validation import analyze_capture
    root=Path(__file__).resolve().parents[1]
    rows=json.loads((root/'tests/pdf/expected_decisions.json').read_text(encoding='utf8'))['fixtures']
    capture_path=Path(os.getenv('PDF_CAPTURE_FILE',str(root.parent/'implementation-evidence/pdf-corpus.json')))
    pdf_dir=Path(os.getenv('PDF_FIXTURE_DIR',str(root.parent/'implementation-evidence/pdf-fixtures')))
    if not capture_path.exists():pytest.skip('Explicit 193-document corpus required')
    captures={r['prefix']:r['content'] for r in json.loads(capture_path.read_text(encoding='utf-8-sig'))}
    monkeypatch.setattr(RouterAI,'extract',lambda *args:pytest.fail('AI invoked while OFF'))
    seen=set()
    with session_factory() as db:
        with db.begin():
            source=EmailSource(email='synthetic@example.invalid',is_active=True)
            db.add(source);db.flush();source_id=source.id
    for expected in rows:
        content=captures[expected['prefix']]
        decision=analyze_capture(content['text'],content['tables'],expected['context'])
        path=pdf_dir/(expected['prefix']+'.pdf')
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected['sha256']
        with session_factory() as db:
            with db.begin():
                serial=decision.get('calculator_number')
                if serial and serial not in seen:
                    db.add(Object(name='Synthetic corpus object',name_norm=uuid.uuid4().hex,calculator_number=serial,
                        email='client-'+hashlib.sha256(serial.encode()).hexdigest()[:12]+'@example.invalid',is_active=True));seen.add(serial)
                message=IncomingMessage(source_id=source_id,from_email='synthetic@example.invalid',provider_message_id=uuid.uuid4().hex)
                db.add(message);db.flush()
                attachment=Attachment(message_id=message.id,filename='fixture.pdf',file_path=str(path),file_sha256=expected['sha256'])
                db.add(attachment);db.flush()
                revision=ProcessingRevision(attachment_id=attachment.id,request_key=uuid.uuid4().hex,request_hash='a'*64,
                    state='received',content_sha256=expected['sha256'],file_path=str(path))
                db.add(revision);db.flush();attachment.current_revision_id=revision.id;revision_id=revision.id
        actual=validate_revision(session_factory,revision_id,analyzer=lambda *a,**kw:copy.deepcopy(decision))
        assert actual['state']==expected['expected_state'],expected['prefix']
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AIRequest))==0
        for delivery,revision in db.execute(select(RecipientDelivery,ProcessingRevision).join(
                DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id).join(
                ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id)):
            if delivery.role=='client':assert revision.state=='validated' and revision.object_id is not None
