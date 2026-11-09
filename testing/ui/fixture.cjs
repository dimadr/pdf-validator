/* Synthetic UI API only. Never connects to a database, broker or mail service. */
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const uuid = n => `cb962740-7000-4000-8000-${String(n).padStart(12, '0')}`;
const longEmail = 'accounting.department.for.monthly.heat.reports@regional-resource-organisation.example.invalid';
const address = 'Ивановская область, городской округ Примерный, проспект Машиностроителей, дом 128, корпус 4, административное здание';
const objects = Array.from({length:105}, (_, i) => ({id:uuid(i+1),eldis_id:i===0?uuid(1):String(10400+i),object_date:'2026-10',
    name:i===0?'Производственный комплекс «Северный», центральный тепловой пункт':`Тепловой пункт № ${i+1}`,
    calculator_number:String(111125000+i),address:i===0?address:`ул. Примерная, ${i+1}`,
    email:i===0?longEmail:`reports-${i+1}@example.invalid`,is_active:i%7!==6}));
const states=['validated','review_required','rejected','technical_error','discarded_inactive','processing'];
const documents=Array.from({length:105},(_,i)=>({id:uuid(1000+i),filename:`Отчёт_теплоснабжение_октябрь_2026_${i+1}.pdf`,
    file_size:48204,status:'validated',created_at:'2026-10-30T09:10:00',
    current_revision:{id:uuid(2000+i),state:states[i%states.length],calculator_number:objects[i].calculator_number,
        object_name:objects[i].name,reason:i%states.length===1?'Не удалось однозначно определить полный номер прибора; требуется проверка оператора':null},
    message:{from_email:i===0?longEmail:'reports@sender.example.invalid',subject:'Показания приборов за октябрь 2026'}}));

function createFixture(root) {
    const state={calls:[],ai:false,role:'admin',aiFailure:false};
    const send=(res,data,status=200,total)=>{
        res.writeHead(status,{'Content-Type':'application/json; charset=utf-8',...(total===undefined?{}:{'X-Total-Count':String(total)})});
        res.end(JSON.stringify(data));
    };
    const server=http.createServer(async(req,res)=>{
        const url=new URL(req.url,'http://127.0.0.1'),p=url.pathname;
        const chunks=[];for await(const chunk of req)chunks.push(chunk);
        const text=Buffer.concat(chunks).toString();let body;try{body=text?JSON.parse(text):null;}catch{send(res,{detail:'Invalid fixture request'},400);return;}
        if(p==='/health/ready'){send(res,{pipeline_v2_enabled:true});return;}
        if(p.startsWith('/api/v1/')){
            state.calls.push({path:p,query:Object.fromEntries(url.searchParams),method:req.method,body,authorized:req.headers.authorization==='Bearer ui-fixture-only'});
            if(p.endsWith('/auth/login')){send(res,{access_token:'ui-fixture-only'});return;}
            if(p.endsWith('/auth/me')){send(res,{id:uuid(5000),username:'Администратор',role:state.role});return;}
            if(p.endsWith('/reports/summary')){send(res,{total_attachments:105,processed:82,rejected:9,smtp_accepted:64,legacy_sent:17});return;}
            if(p.endsWith('/pipeline/status')){send(res,{pipeline_v2_enabled:true,recipient_states:{accepted:64,pending:3,in_flight:0,failed:2,uncertain:0},pending_outbox:3,technical_errors:0});return;}
            if(p.endsWith('/settings/ai-fallback')){
                if(req.method==='PUT'){
                    if(state.aiFailure){send(res,{detail:'Провайдер недоступен (тестовый ответ)'},503);return;}
                    state.ai=body.enabled;
                }
                send(res,{requested:state.ai,availability:state.ai?'enabled':'disabled'});return;
            }
            if(p.endsWith('/settings/email-config')){send(res,{imap_server:'imap.example.invalid',imap_port:993,imap_ssl:true,smtp_server:'smtp.example.invalid',smtp_port:465,smtp_ssl:true,email_username:'processor@example.invalid',email_from_name:'Теплоснабжение',editable:false});return;}
            if(/\/attachments\/[^/]+\/details$/.test(p)){
                send(res,{...documents[0],current_revision_id:uuid(2000),current_revision:{...documents[0].current_revision,validation_result:{ai:{state:'disabled',attempts:[]}}},validation_result:{calculator_number:'111125000',status:'validated',notes:['Синтетические данные интерфейса']}});return;
            }
            if(/\/attachments\/[^/]+\/deliveries$/.test(p)){
                const skip=Number(url.searchParams.get('skip')||0);
                send(res,{total:26,items:Array.from({length:skip?1:25},(_,i)=>({recipient:i===0?longEmail:`client-${skip+i}@example.invalid`,role:i===1?'admin':'client',state:['accepted','pending','failed','uncertain'][i%4],attempts:1,message_id:`<${uuid(8000+skip+i)}@processor.example.invalid>`,error:i%4===2?'550 Recipient address rejected: synthetic UI fixture':'',accepted_at:i%4===0?'2026-10-30T09:12:00Z':null}))});return;
            }
            const oneObject=p.match(/\/objects\/([^/]+)$/);
            if(oneObject&&req.method==='GET'){send(res,objects.find(x=>x.id===oneObject[1])||objects[0]);return;}
            if(req.method!=='GET'){send(res,{id:uuid(9999),state:'queued',...body});return;}
            if(/\/objects\/$/.test(p)){
                let rows=objects.filter(x=>!url.searchParams.get('search')||x.name.toLowerCase().includes(url.searchParams.get('search').toLowerCase()));
                const total=rows.length,skip=Number(url.searchParams.get('skip')||0);
                send(res,rows.slice(skip,skip+100),200,total);return;
            }
            if(p.endsWith('/email-sources/')){send(res,[{id:uuid(6000),email:longEmail,name:'Автоматическая выгрузка показаний',is_active:true},{id:uuid(6001),email:'archive@sender.example.invalid',name:'Архивный источник',is_active:false}]);return;}
            if(p.endsWith('/attachments')){
                const search=url.searchParams.get('search')||'',status=url.searchParams.get('status');
                const rows=documents.filter(x=>(!search||x.filename.includes(search))&&(!status||x.current_revision.state===status));
                const skip=Number(url.searchParams.get('skip')||0);send(res,rows.slice(skip,skip+100),200,rows.length);return;
            }
            if(p.endsWith('/reports/rejections')){send(res,documents.slice(0,7).map((x,i)=>({filename:x.filename,reject_reason:i===0?'Неполный период: отсутствуют измерения за 31.10.2026':'Некорректные значения измерений',object_name:objects[i].name,message_subject:'Отчёт о потреблении тепловой энергии за октябрь 2026 года',from_email:longEmail,created_at:x.created_at})),200,7);return;}
            if(p.endsWith('/auth/users')){send(res,[{id:uuid(5000),username:'Администратор',email:'admin@example.invalid',role:'admin',is_active:true},{id:uuid(5001),username:'Оператор',email:longEmail,role:'operator',is_active:true}]);return;}
            if(p.endsWith('/settings/trusted-emails')){send(res,[{id:uuid(7000),email:longEmail,description:'Доверенный адрес отправителя отчётов по тепловой энергии',is_active:true}]);return;}
            send(res,{detail:`Unhandled fixture endpoint: ${p}`},404);return;
        }
        const parts=decodeURIComponent(p).split('/').filter(Boolean),variant=parts.shift();
        if(!['before','after'].includes(variant)){res.writeHead(404);res.end();return;}
        const base=variant==='before'?path.join(root,'.testing-artifacts/ui/before'):path.join(root,'ui');
        const filename=path.resolve(base,...(parts.length?parts:['index.html']));
        if(!filename.startsWith(base+path.sep)||!fs.existsSync(filename)||!fs.statSync(filename).isFile()){res.writeHead(404);res.end();return;}
        const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.woff2':'font/woff2','.woff':'font/woff','.svg':'image/svg+xml'};
        res.writeHead(200,{'Content-Type':types[path.extname(filename)]||'application/octet-stream','Cache-Control':'no-store'});fs.createReadStream(filename).pipe(res);
    });
    return {server,state,objects,documents,longEmail,address,uuid};
}

module.exports={createFixture};
