"""Bounded structured extraction; model output never chooses a client or verdict."""
import base64, hashlib, json, os, re, time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import select, func, text
from common.models import AIRequest, ProcessingRevision, Attachment, utcnow
from common.ai_settings import control, permitted, boolean_env, LOCK

PROMPT_VERSION = 'extract-evidence/1'
TRIGGERS = {'identity_missing','identity_ambiguous','unknown_instrument_format','unknown_table_signature',
            'zero_checked_rows','daily_series_missing'}

class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Evidence(Contract):
    page: int = Field(ge=1)
    quote: StrictStr = Field(min_length=1, max_length=4000)

class Row(Contract):
    cells: list[StrictStr | None] = Field(min_length=2,max_length=80)
    evidence: Evidence

class Series(Contract):
    headers: list[StrictStr] = Field(min_length=2,max_length=80)
    required_columns: list[int]
    rows: list[Row] = Field(min_length=1,max_length=1000)
    evidence: Evidence

class Extraction(Contract):
    serial_candidates: list[StrictStr] = Field(max_length=8)
    instrument_model: StrictStr
    billing_period: list[StrictStr] = Field(min_length=2,max_length=2)
    series: list[Series] = Field(min_length=1,max_length=30)
    evidence: list[Evidence] = Field(min_length=1,max_length=20)
    ambiguities: list[StrictStr]
    parser_engine: Literal['cloudflare-ai','mistral-ocr','native']
    prompt_version: Literal['extract-evidence/1']

class ProviderError(Exception):
    def __init__(self,code,unknown=False): self.code=code; self.unknown=unknown

class RouterAI:
    def extract(self, content, model, engine):
        if os.getenv('AI_PROVIDER_MODE')=='stub' and boolean_env('SMTP_TEST_MODE'):
            # Fixed loopback only: staging stub cannot relay documents outside its container.
            try:
                response=httpx.post('http://127.0.0.1:8099/chat/completions',
                    json={'model':model,'parser_engine':engine,'file_data':base64.b64encode(content).decode()},timeout=45,trust_env=False)
                if response.status_code!=200:raise ProviderError('http_'+str(response.status_code))
                payload=response.json();return payload['extraction'],payload.get('usage',{'cost':'unknown'})
            except httpx.TimeoutException:raise ProviderError('timeout',True) from None
        # Real document transmission needs a separately recorded operator authorization.
        if not boolean_env('AI_EXTERNAL_DOCUMENTS_APPROVED'):
            raise ProviderError('external_consent_required')
        base=os.getenv('ROUTERAI_BASE_URL','https://routerai.ru/api/v1').rstrip('/')
        if base != 'https://routerai.ru/api/v1': raise ProviderError('provider_url_not_allowed')
        timeout=min(45,max(1,int(os.getenv('AI_TIMEOUT_SECONDS','45'))))
        headers={'Authorization':'Bearer '+os.getenv('ROUTERAI_API_KEY','')}
        try:
            with httpx.Client(timeout=timeout,follow_redirects=False,trust_env=False) as client:
                listing=client.get(base+'/models',headers=headers)
                if listing.status_code != 200: raise ProviderError('model_listing_http_'+str(listing.status_code))
                if model not in {m['id'] for m in listing.json().get('data',[])}: raise ProviderError('model_unavailable')
                schema=Extraction.model_json_schema()
                prompt=('Extract only what is printed. Never infer missing measurements, choose a recipient, or decide validity. '
                        'Every header and measurement row needs its exact complete printed line and 1-based page. '
                        'Keep full serials, leading zeros, all columns and every dated row. Ambiguities must be explicit. '
                        'Return JSON matching this schema: '+json.dumps(schema))
                response=client.post(base+'/chat/completions',headers=headers,json={
                    'model':model,'temperature':0,'response_format':{'type':'json_object'},
                    'messages':[{'role':'user','content':[{'type':'text','text':prompt},
                        {'type':'file','file':{'filename':'document.pdf','file_data':'data:application/pdf;base64,'+base64.b64encode(content).decode()}}]}],
                    'plugins':[{'id':'file-parser','pdf':{'engine':engine}}]})
                if response.status_code != 200: raise ProviderError('http_'+str(response.status_code))
                payload=response.json()
                return json.loads(payload['choices'][0]['message']['content']),payload.get('usage',{'cost':'unknown'})
        except httpx.TimeoutException: raise ProviderError('timeout',True) from None
        except httpx.TransportError: raise ProviderError('transport_unknown',True) from None
        except (ValueError,KeyError,TypeError): raise ProviderError('invalid_response') from None

def needs_ai(result):
    # A confirmed defect is immutable evidence; extraction assistance cannot erase it.
    return result.get('state')=='review_required' and not any(i.get('severity')=='rejected' for i in result.get('issues',[])) and (
        result.get('identity_state') in ('missing','ambiguous') or bool(TRIGGERS.intersection(result.get('reason','').split(';'))))

def _norm(value): return re.sub(r'\s+',' ',str(value or '')).strip()

def verify(path, extracted, original, context):
    """Conservative independent text evidence; image-only OCR stays in review."""
    import pdfplumber
    from common.pdf_validation import analyze_capture, DATE_TOKEN
    data=Extraction.model_validate(extracted)
    with pdfplumber.open(path) as pdf:
        pages=[[_norm(line) for line in (p.extract_text() or '').splitlines()] for p in pdf.pages]
    def line(evidence):
        if evidence.page>len(pages) or _norm(evidence.quote) not in pages[evidence.page-1]:
            raise ValueError('evidence_not_in_source')
        return _norm(evidence.quote)
    anchors=[line(e) for e in data.evidence]
    source='\n'.join('\n'.join(p) for p in pages)
    if data.ambiguities or len(data.serial_candidates)!=1: raise ValueError('identity_ambiguous')
    serial=data.serial_candidates[0]
    if not re.fullmatch(r'[0-9A-Za-zА-Яа-я-]{4,50}',serial): raise ValueError('invalid_serial')
    serial_label=r'(?:Заводской\s+номер|Номер\s+прибора|Серийный\s+номер|Serial(?:\s+Number|\s+No\.?)?)\s*[:№=]?\s*'
    labelled={m.group(1) for m in re.finditer(serial_label+r'([0-9A-Za-zА-Яа-я][0-9A-Za-zА-Яа-я-]{3,49})(?![\w-])',source,re.I)}
    if labelled!={serial} or not any(re.search(serial_label+re.escape(serial)+r'(?![\w-])',a,re.I) for a in anchors):
        raise ValueError('serial_evidence_missing')
    existing=original.get('serial_candidates',[])
    if len(existing)>1 or existing and serial not in existing: raise ValueError('serial_conflict')
    if not any(data.instrument_model in a for a in anchors): raise ValueError('model_evidence_missing')
    tables=[];covered=Counter()
    if len(data.series)<len(original.get('series',[])):raise ValueError('unproven_series_coverage')
    for series in data.series:
        header=line(series.evidence)
        if series.required_columns != list(range(1,len(series.headers))): raise ValueError('required_columns_incomplete')
        if not all(_norm(h) and _norm(h) in header for h in series.headers): raise ValueError('header_evidence_missing')
        table=[series.headers]
        for row in series.rows:
            quoted=line(row.evidence)
            if len(row.cells)!=len(series.headers): raise ValueError('row_width')
            # Exact ordered token sequence prevents swapping fields or inventing zeros.
            if ' '.join(_norm(c) for c in row.cells) != quoted: raise ValueError('row_evidence_mismatch')
            covered[(row.evidence.page,quoted)]+=1;table.append(row.cells)
        tables.append(table)
    printed=Counter((p+1,s) for p,lines in enumerate(pages) for s in lines if re.match(r'^'+DATE_TOKEN+r'\s',s))
    if not printed or printed != covered: raise ValueError('unproven_series_coverage')
    result=analyze_capture(source+'\nЗаводской номер: '+serial,tables,context)
    # Validate PDF-declared period independently; never invent a billing window from AI.
    periods={tuple(s['period'][k] for k in ('start','end')) for s in result['series'] if s.get('period')}
    if periods!={tuple(data.billing_period)}: raise ValueError('period_evidence_mismatch')
    result['ai_extraction_verified']=True
    return result

def run_fallback(factory, revision_id, original, context, provider=None):
    if not needs_ai(original): return original
    provider=provider or RouterAI()
    with factory() as db:
        revision=db.get(ProcessingRevision,revision_id)
        if not permitted(db,revision=revision):
            return {**original,'ai':{'state':'disabled','reason':'not_enabled_for_revision'}}
        generation=control(db).generation
        attachment=db.get(Attachment,revision.attachment_id)
        scope=str(attachment.message.source_id)
        sha=revision.content_sha256;path=revision.file_path
    engine=os.getenv('AI_PARSER_ENGINE','cloudflare-ai')
    if engine not in ('cloudflare-ai','mistral-ocr','native'):return {**original,'ai':{'state':'failed','reason':'invalid_parser_engine'}}
    models=[os.getenv('AI_MODEL_PRIMARY','openai/gpt-4o-mini')]
    if os.getenv('AI_ESCALATION_ENABLED','true').lower()=='true': models.append(os.getenv('AI_MODEL_SECONDARY','openai/gpt-6-luna-pro'))
    models=models[:min(2,max(0,int(os.getenv('AI_MAX_CALLS_PER_REVISION','2'))))]
    attempts=[]
    for model in models:
        key=hashlib.sha256(json.dumps([scope,sha,model,engine,PROMPT_VERSION,context],sort_keys=True,default=str).encode()).hexdigest()
        with factory() as db:
            with db.begin():
                if not permitted(db,generation=generation,lock=True): break
                current_revision=db.get(ProcessingRevision,revision_id)
                parent=db.get(Attachment,current_revision.attachment_id)
                if parent.current_revision_id!=revision_id:break
                # Single global lock also bounds daily reservations across worker processes.
                cached=db.scalar(select(AIRequest).where(AIRequest.cache_key==key))
                if cached is None:
                    current=db.scalar(select(func.count()).select_from(AIRequest).where(AIRequest.revision_id==revision_id))
                    midnight=utcnow().replace(hour=0,minute=0,second=0,microsecond=0)
                    daily=db.scalar(select(func.count()).select_from(AIRequest).where(AIRequest.created_at>=midnight))
                    if current>=2 or daily>=min(20,max(0,int(os.getenv('AI_DAILY_REQUEST_LIMIT','20')))):
                        attempts.append({'state':'failed','reason':'request_limit'});break
                    cached=AIRequest(revision_id=revision_id,cache_key=key,content_sha256=sha,model=model,
                        parser_engine=engine,prompt_version=PROMPT_VERSION,state='reserved',generation=generation,reason=original['reason'])
                    db.add(cached);db.flush();fresh=True
                    current_revision.validation_result={**(current_revision.validation_result or {}),
                        'ai':{'state':'pending','model':model,'reason':original['reason'],'generation':generation}}
                else: fresh=False
                request_id=cached.id;state=cached.state;extracted=cached.result;usage=cached.usage;error=cached.error_code
        if fresh:
            start=time.monotonic()
            # Last live check immediately before external effect. An already transmitted call cannot be revoked.
            with factory() as db:
                allowed=permitted(db,generation=generation)
            try:
                if not allowed:raise ProviderError('disabled_before_call')
                content=Path(path).read_bytes()
                if hashlib.sha256(content).hexdigest()!=sha:raise ProviderError('content_integrity')
                extracted,usage=provider.extract(content,model,engine)
                extracted=Extraction.model_validate(extracted).model_dump()
                if extracted['parser_engine']!=engine:raise ProviderError('parser_engine_mismatch')
                state,error='complete',None
            except ProviderError as exc:state,error=('unknown' if exc.unknown else 'failed'),exc.code;extracted=None;usage={'cost':'unknown'}
            except Exception:state,error='failed','invalid_response';extracted=None;usage={'cost':'unknown'}
            with factory() as db:
                with db.begin():
                    row=db.get(AIRequest,request_id);row.state=state;row.error_code=error;row.result=extracted
                    row.usage=usage;row.elapsed_ms=int((time.monotonic()-start)*1000);row.finished_at=utcnow()
        attempts.append({'request_id':str(request_id),'model':model,'state':state,'cache_hit':not fresh,'usage':usage or {'cost':'unknown'}})
        with factory() as db:
            if not permitted(db,generation=generation): break
        if state=='complete':
            try:
                result=verify(path,extracted,original,context)
                return {**result,'deterministic_result':original,'ai':{'state':'used','generation':generation,
                    'reason':original['reason'],'attempts':attempts,'parser_engine':engine,'prompt_version':PROMPT_VERSION}}
            except (ValueError,TypeError,KeyError):attempts[-1]['verification']='evidence_or_contract_failed'
        # Unknown timeout may have been billed: never retry or escalate it automatically.
        if state in ('unknown','reserved') or error in ('external_consent_required','http_400','http_401','http_403','http_429','disabled_before_call'):break
    return {**original,'ai':{'state':'failed','reason':'unresolved_or_disabled','generation':generation,'attempts':attempts}}
