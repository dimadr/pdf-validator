const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const root = path.resolve(__dirname, '..');

class Node {
    constructor() { this.attrs={}; this.fields={}; this.listeners={}; this.value=''; this.valid=true; this.onclick=null; }
    addEventListener(type, fn) { (this.listeners[type]??=[]).push(fn); }
    querySelector(selector) { return this.fields[selector]??=(new Node()); }
    setAttribute(name,value) { this.attrs[name]=value; }
    getAttribute(name) { return this.attrs[name]??null; }
    removeAttribute(name) { delete this.attrs[name]; }
    reset() { for(const field of Object.values(this.fields)) field.value=''; }
    reportValidity() { return this.valid; }
    insertRow() { return new Node(); }
    async dispatch(type) {
        const work=(this.listeners[type]||[]).map(fn=>fn({preventDefault(){}}));
        if(type==='click'&&this.onclick) work.push(this.onclick({}));
        await Promise.allSettled(work);
    }
}

function fixture(fetch=async()=>{throw Error('Unexpected fetch')}) {
    const nodes=new Map(),storage=new Map(),alerts=[];
    const node=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
    class Modal {constructor(element){this.element=element;}show(){}hide(){this.element.dispatch('hidden.bs.modal');}static getInstance(element){return new Modal(element);}}
    const context={console,crypto,fetch,Uint8Array,URLSearchParams,
        document:{getElementById:node,querySelector:node},bootstrap:{Modal},setTimeout:()=>0,
        sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
        FormData:class {constructor(form){this.form=form;}get(name){return this.form.querySelector(`input[name="${name}"]`).value;}}};
    vm.runInNewContext(fs.readFileSync(path.join(root,'ui/pipeline.js'),'utf8'),context);
    vm.runInNewContext(fs.readFileSync(process.env.UI_REGRESSION_SOURCE||path.join(root,'ui/app.js'),'utf8').replace('const admin = new EmailProcessorAdmin();','globalThis.AdminClass = EmailProcessorAdmin;'),context);
    const admin=Object.create(context.AdminClass.prototype);
    Object.assign(admin,{apiBase:'/api/v1',token:'test-only',currentUser:{role:'admin'},pipelineEnabled:true,
        pageSize:100,objectSkip:0,documentSkip:0,reportSkip:0});
    admin.showAlert=(message,type)=>alerts.push({message,type});
    admin.pipeline=new context.PipelineUI(admin);
    return {admin,node,alerts,storage,context};
}

test('AI switch OFF ON OFF uses authenticated API without restart and never exposes a key',async()=>{
    const calls=[];
    const f=fixture();
    f.admin.apiCall=async (path,options)=>{calls.push({path,options});const requested=options?JSON.parse(options.body).enabled:false;
        return {requested,availability:requested?'enabled':'disabled'};};
    await f.admin.loadAIConfig();
    assert.equal(f.node('ai-fallback-toggle').checked,false);
    f.node('ai-fallback-toggle').checked=true;await f.node('ai-fallback-toggle').onchange();
    assert.equal(f.node('ai-requested').textContent,'ВКЛ');
    f.node('ai-fallback-toggle').checked=false;await f.node('ai-fallback-toggle').onchange();
    assert.equal(f.node('ai-requested').textContent,'ВЫКЛ');
    assert.equal(calls.length,3);
    assert.ok(calls.every(c=>c.path==='/settings/ai-fallback'));
});

test('document table distinguishes checked PDF from completed client SMTP and hides positive rejection reason',async()=>{
    const f=fixture(),rows=[];
    f.admin.renderPager=()=>{};f.admin.renderEmptyState=()=>{};
    f.node('#documents-table tbody').insertRow=()=>{const row=new Node();rows.push(row);return row;};
    f.admin.apiCall=async()=>({total:1,items:[{id:'fixture-doc',filename:'report.pdf',status:'new',created_at:'2026-11-03T06:41:54',
        current_revision:{state:'validated',reason:'validated'},client_delivery:{counts:{accepted:2}},recipient_state_counts:{client:{accepted:2}}}]});
    await f.admin.loadDocuments();
    assert.match(rows[0].innerHTML,/Проверен и отправлен/);
    assert.doesNotMatch(rows[0].innerHTML,/data-value="validated"/);
});

test('sent label requires every current client and never administrative or previous-operation success',()=>{
    const {admin}=fixture();
    const item=counts=>({status:'new',current_revision:{state:'validated'},client_delivery:{counts}});
    for(const [counts,status] of [
        [{accepted:2},'validated_sent'],[{accepted:1,pending:1},'validated_partial'],
        [{accepted:1,uncertain:1},'validated_uncertain'],[{pending:2},'validated_pending'],
        [{failed:1},'validated_failed'],[{in_flight:1},'validated_sending'],[{},'validated']]) {
        assert.equal(admin.documentStatus(item(counts)),status);
    }
    const later=item({pending:1});later.recipient_state_counts={client:{accepted:5},admin:{accepted:2}};
    assert.equal(admin.documentStatus(later),'validated_pending');
    assert.equal(admin.documentStatus({status:'new',current_revision:{state:'validated'},recipient_state_counts:{admin:{accepted:1}}}),'validated');
    assert.equal(admin.documentStatus({...item({accepted:2}),status:'archived'}),'archived');
});

test('document rows show independent delivery and MDN indicators',async()=>{
    const f=fixture(),rows=[];f.admin.renderPager=()=>{};f.admin.renderEmptyState=()=>{};
    f.node('#documents-table tbody').insertRow=()=>{const row=new Node();rows.push(row);return row;};
    const cases=[
        ['Mail.ru',1,1,1,1,true,true], ['Яндекс',1,1,0,1,true,false],
        ['SMTP ACK',0,0,0,1,false,false], ['Brevo Opened',0,0,0,1,false,false],
        ['MDN',1,0,1,1,true,true], ['Разные адресаты',2,1,1,2,true,false],
        ['Частичная доставка',1,1,0,2,false,false],
    ];
    const item=([filename,confirmed,brevo_delivered,read_confirmed,total])=>({id:'fixture',filename,status:'new',created_at:'2026-11-03T06:41:54',
        message:{from_email:'noreply@eldis24.ru'},current_revision:{state:'validated',reason:'validated'},
        client_delivery:{counts:{accepted:total},providers:['brevo'],confirmation:{confirmed,brevo_delivered,read_confirmed,total}}});
    f.admin.apiCall=async()=>({total:cases.length,items:cases.map(item)});await f.admin.loadDocuments();
    assert.match(rows[0].innerHTML,/Brevo/);
    assert.doesNotMatch(rows[0].innerHTML,/noreply@eldis24.ru/);
    for (const [i,[label,,,,,delivery,read]] of cases.entries()) {
        const actual=[...rows[i].innerHTML.matchAll(/data-signal="(delivery|read)"[^>]*>\s*<span class="confirmation-dot (confirmation-present|confirmation-missing)"/g)];
        assert.deepEqual(actual.map(m=>[m[1],m[2]==='confirmation-present']),[['delivery',delivery],['read',read]],label);
    }
    assert.match(rows[0].innerHTML,/Brevo Delivered — 1, MDN — 1/);
    assert.match(rows[4].innerHTML,/MDN — 1/);
    assert.match(rows[6].innerHTML,/Доставка подтверждена для 1 из 2/);
});

test('SMTP selection requires confirmation, retains expected provider and hides controls from operators',async()=>{
    const f=fixture(),calls=[];
    f.node('smtp-provider-select').options=[{value:'brevo'},{value:'timeweb'}];
    f.context.window={confirm:()=>false};
    f.admin.apiCall=async(path,options)=>{
        calls.push({path,options});return {provider:options?JSON.parse(options.body).provider:'timeweb',
            available:{brevo:true,timeweb:true},brevo_used_24h:0,brevo_limit_24h:300,last_change:null};
    };
    await f.admin.loadSMTPConfig();
    f.node('smtp-provider-select').value='brevo';await f.node('smtp-provider-apply').onclick();
    assert.equal(calls.length,1);
    f.context.window.confirm=()=>true;
    f.node('smtp-provider-select').value='brevo';await f.node('smtp-provider-apply').onclick();
    assert.deepEqual(JSON.parse(calls[1].options.body),{provider:'brevo',expected_provider:'timeweb',confirmed:true});
    assert.equal(f.node('smtp-active-provider').textContent,'Brevo');
    assert.equal(f.node('smtp-provider-apply').disabled,false);
    f.admin.currentUser.role='operator';await f.admin.loadSMTPConfig();assert.equal(calls.length,2);
});

test('Authorization and Idempotency-Key survive together; page metadata is returned',async()=>{
    let observed;
    const {admin}=fixture(async(url,options)=>{
        observed={url,options};return {ok:true,json:async()=>[],headers:{get:()=> '105'}};
    });
    const page=await admin.apiCall('/attachments',{page:true,headers:{'Idempotency-Key':'request-fixture'}});
    assert.equal(observed.options.headers.Authorization,'Bearer test-only');
    assert.equal(observed.options.headers['Idempotency-Key'],'request-fixture');
    assert.equal(observed.options.page,undefined);
    assert.equal(page.total,105);
});

test('uncertain HTTP result retries same key and original expected revision',async()=>{
    const {admin,storage}=fixture();const posts=[];let gets=0;
    admin.loadDocuments=async()=>{};
    admin.apiCall=async(endpoint,options={})=>{
        if(!options.method){gets++;return {current_revision_id:gets===1?null:'new-revision'};}
        posts.push({key:options.headers['Idempotency-Key'],body:JSON.parse(options.body)});
        if(posts.length===1){const error=Error('Temporary result failure');error.status=503;throw error;}
        return {state:'queued'};
    };
    await admin.pipeline.command('reprocess','fixture-id');
    assert.equal(storage.size,1);
    await admin.pipeline.command('reprocess','fixture-id');
    assert.equal(gets,1);
    assert.equal(posts[0].key,posts[1].key);
    assert.equal(posts[1].body.expected_revision_id,null);
    assert.equal(storage.size,0);
    await admin.pipeline.command('reprocess','fixture-id');
    assert.notEqual(posts[1].key,posts[2].key);
    assert.equal(posts[2].body.expected_revision_id,'new-revision');
});

test('double click creates one command while a disabled pipeline creates none',async()=>{
    const {admin}=fixture();let posts=0;admin.loadDocuments=async()=>{};
    admin.apiCall=async(endpoint,options={})=>{
        if(!options.method){await Promise.resolve();return {current_revision_id:null};}
        posts++;return {state:'queued'};
    };
    await Promise.all([admin.pipeline.command('resend','fixture-id'),admin.pipeline.command('resend','fixture-id')]);
    assert.equal(posts,1);
    admin.pipelineEnabled=false;
    await admin.pipeline.command('resend','fixture-id');
    assert.equal(posts,1);
});

test('recipient history labels administrative role and escapes all server strings',async()=>{
    const {admin}=fixture();
    admin.apiCall=async()=>({total:1,items:[{recipient:'<img src=x>',role:'admin',state:'accepted',attempts:1,
        message_id:'<fixture@example.com>',error:'<script>unsafe</script>'}]});
    const html=await admin.pipeline.historyHTML('fixture-id');
    assert.ok(html.includes('Администратор'));
    assert.ok(html.includes('Принято SMTP'));
    assert.ok(html.includes('&lt;img'));
    assert.ok(!html.includes('<script>'));
    assert.ok(html.includes('Доставка означает принятие сервером получателя'));
});

test('source edit has one PUT, next create one POST; invalid object form sends nothing',async()=>{
    const {admin,node}=fixture();const calls=[];
    admin.loadEmailSources=async()=>{};admin.loadObjects=async()=>{};
    admin.apiCall=async(endpoint,options={})=>{calls.push({endpoint,method:options.method||'GET'});
        return {id:'source-id',email:'source@example.com',name:'Source'};};
    admin.setupEventListeners();await admin.editEmailSource('source-id');
    await node('save-email-source').dispatch('click');
    assert.deepEqual(calls.map(x=>x.method),['GET','PUT']);
    await node('email-source-modal').dispatch('hidden.bs.modal');
    const form=node('email-source-form');form.querySelector('input[name="email"]').value='new@example.com';
    await node('save-email-source').dispatch('click');
    assert.equal(calls.at(-1).method,'POST');
    const count=calls.length;node('object-form').valid=false;await node('save-object').dispatch('click');
    assert.equal(calls.length,count);
});

test('Brevo delivery and read confirmation render independently with admin event history',async()=>{
    const {admin}=fixture();
    admin.apiCall=async endpoint=>endpoint.includes('/delivery-events?')
        ? {total:1,items:[{event:'blocked',provider:'brevo',reason:'<script>bad</script>',smtp_attempt:1}]}
        : {total:2,items:[{id:'delivery-id',recipient:'one@example.com',role:'client',state:'accepted',
            delivery:{status:'delivered',event_count:1,opened_at:'2026-11-09T08:33:00Z'}},
            {id:'mdn-id',recipient:'two@example.com',role:'client',state:'accepted',delivery:{status:'unknown'},
                read_confirmed_at:'2026-11-09T08:35:00Z'}]};
    const html=await admin.pipeline.historyHTML('test');
    assert.match(html,/<th>Доставка<\/th><th>Прочтение<\/th>/);
    assert.match(html,/Доставлено на почтовый сервер/);
    assert.match(html,/Нет подтверждения/);
    assert.match(html,/Сигнал открытия Brevo/);
    assert.match(html,/Доставка подтверждена по MDN/);
    assert.match(html,/js-brevo-events/);
    assert.doesNotMatch(await admin.pipeline.brevoEventsHTML('test','delivery-id',0),/<script>/);
    admin.currentUser.role='operator';
    assert.doesNotMatch(await admin.pipeline.historyHTML('test'),/js-brevo-events/);
});

test('object list requests later pages and a new search resets its offset',async()=>{
    const {admin}=fixture();const urls=[];admin.renderPager=()=>{};
    admin.apiCall=async(endpoint)=>{urls.push(endpoint);return {items:[],total:105};};
    admin.objectSkip=100;await admin.loadObjects(false);await admin.loadObjects();
    assert.ok(urls[0].includes('skip=100'));
    assert.ok(urls[1].includes('skip=0'));
});

test('readiness failure disables manual actions',async()=>{
    const {admin,node}=fixture(async()=>({ok:false}));
    await admin.checkConnection();
    assert.equal(admin.pipelineEnabled,false);
    assert.equal(node('connection-status').textContent,'API или схема не готовы');
});

test('SMTP card uses current client acceptance, never the legacy sent aggregate',async()=>{
    const {admin,node}=fixture();
    admin.apiCall=async endpoint=>endpoint==='/reports/summary'
        ? {total_attachments:105,processed:100,rejected:3,sent:99,smtp_accepted:2}
        : {pipeline_v2_enabled:true,recipient_states:{accepted:4},pending_outbox:0,technical_errors:0};
    await admin.loadStatistics();
    assert.equal(node('sent-attachments').textContent,2);
    assert.ok(fs.readFileSync(path.join(root,'ui/index.html'),'utf8').includes('>Принято SMTP</h5>'));
});
