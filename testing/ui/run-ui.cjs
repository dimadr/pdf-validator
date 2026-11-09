/* Real browser tests against loopback static UI and the synthetic fixture API. */
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {once}=require('node:events');
const root=path.resolve(__dirname,'../..');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= path.join(root,'.testing-artifacts/browsers');
const {chromium,webkit}=require('playwright');
const {createFixture}=require('./fixture.cjs');
const phase=process.argv.includes('--before')?'before':'after';
const engine=process.argv.includes('--webkit')?'webkit':'edge';
if(phase==='before'){
    const baseline=path.join(root,'.testing-artifacts/ui/before');fs.mkdirSync(baseline,{recursive:true});
    for(const file of ['index.html','app.js','pipeline.js','favicon.svg']){
        const source=require('node:child_process').execFileSync('git',['show',`7ed5d40acfb73c4e899254b9ebab2e86d453f846:ui/${file}`],{cwd:root});
        fs.writeFileSync(path.join(baseline,file),source);
    }
}
const output=path.join(root,'evidence/ui',phase,engine);
fs.mkdirSync(output,{recursive:true});
const results=[],screenshots=[],metrics=[],errors=[],unexpected=[];
const fixture=createFixture(root);
const checks=async(name,fn)=>{
    try{await fn();results.push({name,status:'PASS'});}
    catch(error){results.push({name,status:'FAIL',error:error.message});console.error(`FAIL: ${name}: ${error.message}`);}
};

(async()=>{
    fixture.server.listen(0,'127.0.0.1');await once(fixture.server,'listening');
    const origin=`http://127.0.0.1:${fixture.server.address().port}`;
    let browser;
    try{
        browser=await(engine==='webkit'?webkit.launch({headless:true}):chromium.launch({channel:'msedge',headless:true}));
        const context=await browser.newContext({viewport:{width:1280,height:960},locale:'ru-RU',timezoneId:'Europe/Moscow',reducedMotion:'reduce'});
        await context.addInitScript(()=>localStorage.setItem('token','ui-fixture-only'));
        await context.route('**/*',async route=>{
            const url=new URL(route.request().url());
            if(url.origin===origin){await route.continue();return;}
            // Baseline CDN resources are served byte-for-byte from pinned local copies.
            const name=url.pathname.split('/').pop();
            if(phase==='before'&&url.hostname==='cdn.jsdelivr.net'){
                const rel=name.startsWith('bootstrap-icons.woff')?'fonts/'+name:name;
                const filename=path.join(root,'ui/vendor',rel);
                if(fs.existsSync(filename)){await route.fulfill({path:filename});return;}
            }
            unexpected.push(url.origin+url.pathname);await route.abort('blockedbyclient');
        });
        const page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
        page.on('response',r=>{if(r.url().startsWith(origin)&&r.status()>=400&&r.status()!==503)errors.push(`HTTP ${r.status()} ${new URL(r.url()).pathname}`);});
        await page.goto(origin+'/'+phase+'/');
        await page.waitForFunction(()=>document.querySelectorAll('#objects-table tbody tr').length===100&&document.querySelectorAll('#trusted-emails-table tbody tr').length===1);
        await page.evaluate(()=>document.fonts.ready);
        const views=[['objects','#objects-tab'],['sources','#email-sources-tab'],['documents','#documents-tab'],['reports','#reports-tab'],['settings-users','#settings-tab'],['settings-trusted','#trusted-tab'],['settings-mail','#email-out-tab']];
        for(const width of [1280,1440,1920]){
            await page.setViewportSize({width,height:960});
            for(const [name,selector]of views){
                await page.locator(selector).click();
                await page.waitForFunction(()=>!document.getAnimations().some(x=>x.playState==='running'));
                await page.evaluate(()=>window.scrollTo(0,0));
                const data=await page.evaluate(()=>({document:document.documentElement.scrollWidth,viewport:innerWidth,font:getComputedStyle(document.body).fontFamily,fontSize:getComputedStyle(document.body).fontSize,interLoaded:[...document.fonts].filter(f=>f.family==='Inter'&&f.status==='loaded').length,
                    tables:[...document.querySelectorAll('.tab-pane.active .table-responsive')].filter(x=>x.getBoundingClientRect().width).map(x=>({width:x.clientWidth,content:x.scrollWidth,overflow:getComputedStyle(x).overflowX})),
                    controls:[...document.querySelectorAll('.tab-pane.active input,.tab-pane.active select,.tab-pane.active button')].filter(x=>x.getBoundingClientRect().width).filter(x=>{const r=x.getBoundingClientRect();return !x.closest('.table-responsive')&&(r.left<0||r.right>innerWidth+1)}).map(x=>x.id||x.textContent.trim())}));
                metrics.push({width,view:name,...data});
                if(phase==='after')await checks(`${engine} ${width}px ${name}: no page overflow or clipped controls`,async()=>{assert.ok(data.document<=width,`${data.document}>${width}`);assert.deepEqual(data.controls,[]);assert.equal(data.fontSize,'14px');assert.ok(data.font.startsWith('Inter'));assert.ok(data.interLoaded>=2);});
                const filename=`${name}-${width}.png`;await page.screenshot({path:path.join(output,filename)});screenshots.push(`${phase}/${engine}/${filename}`);
            }
        }
        await page.setViewportSize({width:1280,height:960});
        await page.locator('#documents-tab').click();
        await page.locator('#documents-table button[onclick*="showAttachmentDetails"]').first().click();
        await page.waitForSelector('.modal.show .recipient-history');
        await page.screenshot({path:path.join(output,'document-details-1280.png')});screenshots.push(`${phase}/${engine}/document-details-1280.png`);
        if(phase==='after'){
            await checks('details: UUID compact, full value accessible; SMTP pagination retained',async()=>{
                const revision=page.locator('.modal.show [data-value="'+fixture.uuid(2000)+'"]');assert.equal(await revision.count(),1);
                assert.equal((await revision.innerText()).includes(fixture.uuid(2000)),false);
                await revision.click();await page.waitForSelector('#valueModal.show');assert.equal(await page.locator('#full-value').innerText(),fixture.uuid(2000));
                await page.locator('#valueModal .btn-close').click();await page.waitForSelector('.modal.show .recipient-history');assert.equal(await revision.count(),1);
                await page.locator('.modal.show .js-history-page').last().click();await page.waitForFunction(()=>document.querySelectorAll('.recipient-history tbody tr').length===1);
                assert.ok((await page.locator('.recipient-history').innerText()).includes('Всего записей: 26'));
            });
        }
        await page.locator('.modal.show [data-bs-dismiss="modal"]').first().click();await page.waitForSelector('.modal.show',{state:'hidden'});
        if(phase==='after'){
            await checks('wide document table scrolls internally to intact action buttons',async()=>{
                await page.locator('#documents-tab').click();
                const dimensions=await page.evaluate(()=>{const region=document.querySelector('#documents-table').parentElement;region.scrollLeft=region.scrollWidth;const bounds=region.getBoundingClientRect(),last=document.querySelector('#documents-table tbody tr td:last-child button:last-child').getBoundingClientRect();return{page:document.documentElement.scrollWidth,width:innerWidth,overflow:getComputedStyle(region).overflowX,buttonRight:last.right,regionRight:bounds.right};});
                assert.equal(dimensions.page,dimensions.width);assert.equal(dimensions.overflow,'auto');assert.ok(dimensions.buttonRight<=dimensions.regionRight);await page.evaluate(()=>document.querySelector('#documents-table').parentElement.scrollLeft=0);
            });
            await checks('keyboard navigation opens and closes full UUID without truncating its preview',async()=>{
                await page.locator('#objects-tab').click();const id=page.locator('#objects-table .identifier').first();
                assert.equal(await id.innerText(),'cb962740…0001');assert.ok(await id.locator('span').evaluate(x=>x.scrollWidth<=x.clientWidth));
                await id.focus();await page.keyboard.press('Enter');await page.waitForSelector('#valueModal.show');assert.equal(await page.locator('#full-value').innerText(),fixture.uuid(1));await page.keyboard.press('Escape');await page.waitForSelector('#valueModal.show',{state:'hidden'});
                await page.waitForFunction(()=>document.activeElement===document.querySelector('#objects-table .identifier'));
            });
        }
        if(phase==='after'&&engine==='edge'){
            await checks('long email: full dialog and actual clipboard copy',async()=>{
                await page.locator('#email-sources-tab').click();await page.locator('#email-sources-table [data-value]').first().click();
                await page.waitForSelector('#valueModal.show');assert.equal(await page.locator('#full-value').innerText(),fixture.longEmail);
                await context.grantPermissions(['clipboard-read','clipboard-write']);await page.locator('#copy-value').click();
                assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),fixture.longEmail);
                await page.locator('#valueModal .btn-close').click();await page.waitForSelector('#valueModal.show',{state:'hidden'});
            });
            await checks('clipboard denial provides a selectable full-value fallback',async()=>{
                await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied');}}}));
                await page.locator('#email-sources-table [data-value]').first().click();await page.waitForSelector('#valueModal.show');await page.locator('#copy-value').click();
                await page.waitForFunction(()=>document.querySelector('#copy-result').textContent.includes('Cmd+C'));assert.equal(await page.evaluate(()=>getSelection().toString()),fixture.longEmail);
                await page.locator('#valueModal .btn-close').click();await page.waitForSelector('#valueModal.show',{state:'hidden'});await page.evaluate(()=>delete navigator.clipboard);
            });
            await checks('object search, period, sort and pagination survive',async()=>{
                await page.locator('#objects-tab').click();await page.locator('#objects-table-pager button').last().click();
                await page.waitForFunction(()=>document.querySelectorAll('#objects-table tbody tr').length===5);
                await page.locator('#search-objects').fill('Северный');await page.waitForFunction(()=>document.querySelectorAll('#objects-table tbody tr').length===1);
                await page.locator('#period-filter').fill('2026-10');await page.locator('#sort-by').selectOption('calculator_number');await page.getByRole('button',{name:'Фильтр',exact:true}).click();
                assert.ok(fixture.state.calls.some(c=>c.path==='/api/v1/objects/'&&c.query.skip==='0'&&c.query.search==='Северный'&&c.query.period==='2026-10'&&c.query.sort_by==='calculator_number'));
                await page.locator('#search-objects').fill('');await page.waitForFunction(()=>document.querySelectorAll('#objects-table tbody tr').length===100);
            });
            await checks('document search, filter, sort and pagination survive',async()=>{
                await page.locator('#documents-tab').click();await page.locator('#documents-table-pager button').last().click();await page.waitForFunction(()=>document.querySelectorAll('#documents-table tbody tr').length===5);
                await page.locator('#status-filter').selectOption('review_required');await page.waitForFunction(()=>document.querySelectorAll('#documents-table tbody tr').length===18);
                await page.locator('#sort-attachments').selectOption('filename');await page.locator('#search-documents').fill('_2.pdf');await page.waitForFunction(()=>document.querySelectorAll('#documents-table tbody tr').length===1);
                assert.ok(fixture.state.calls.some(c=>c.path==='/api/v1/attachments'&&c.query.skip==='0'&&c.query.status==='review_required'&&c.query.sort_by==='filename'&&c.query.search==='_2.pdf'));
            });
            await checks('object form: edit PUT, invalid create blocked, valid create POST',async()=>{
                await page.locator('#objects-tab').click();await page.locator('#objects-table button[onclick*="editObject"]').first().click();await page.waitForSelector('#objectModal.show');
                assert.equal(await page.locator('#object-form [name="email"]').inputValue(),fixture.longEmail);
                await page.locator('#save-object').click();await page.waitForSelector('#objectModal.show',{state:'hidden'});
                assert.ok(fixture.state.calls.some(c=>c.method==='PUT'&&c.path==='/api/v1/objects/'+fixture.uuid(1)&&c.body.calculator_number==='111125000'));
                await page.locator('[data-bs-target="#objectModal"]').click();const before=fixture.state.calls.length;await page.locator('#save-object').click();assert.equal(fixture.state.calls.length,before);
                await page.locator('#object-form [name="name"]').fill('Синтетический объект');await page.locator('#save-object').click();await page.waitForSelector('#objectModal.show',{state:'hidden'});
                assert.ok(fixture.state.calls.some(c=>c.method==='POST'&&c.path==='/api/v1/objects/'&&c.body.name==='Синтетический объект'));
            });
            await checks('source form still creates one source',async()=>{
                await page.locator('#email-sources-tab').click();await page.locator('[data-bs-target="#email-source-modal"]').click();
                await page.locator('#email-source-form [name="email"]').fill('fixture-new@example.invalid');await page.locator('#email-source-form [name="name"]').fill('Тестовый источник');await page.locator('#save-email-source').click();await page.waitForSelector('#email-source-modal.show',{state:'hidden'});
                assert.equal(fixture.state.calls.filter(c=>c.method==='POST'&&c.path==='/api/v1/email-sources/').length,1);
            });
            await checks('AI settings OFF/ON/OFF and failure recovery use authenticated mock only',async()=>{
                await page.locator('#settings-tab').click();const toggle=page.locator('#ai-fallback-toggle');assert.equal(await toggle.isChecked(),false);
                await toggle.check();await page.waitForFunction(()=>document.querySelector('#ai-requested').textContent==='ВКЛ');await toggle.uncheck();await page.waitForFunction(()=>document.querySelector('#ai-requested').textContent==='ВЫКЛ');
                fixture.state.aiFailure=true;await toggle.check();await page.waitForFunction(()=>!document.querySelector('#ai-fallback-toggle').checked&&!document.querySelector('#ai-fallback-toggle').disabled);fixture.state.aiFailure=false;
                assert.ok(fixture.state.calls.filter(c=>c.path.endsWith('/settings/ai-fallback')).every(c=>c.authorized));
            });
            await checks('managed mail settings, user and trusted-email forms remain available',async()=>{
                await page.locator('#email-out-tab').click();assert.equal(await page.locator('#imap-server').inputValue(),'imap.example.invalid');assert.equal(await page.locator('#imap-server').isDisabled(),true);
                await page.locator('#users-settings-tab').click();await page.locator('[data-bs-target="#userModal"]').click();await page.waitForSelector('#userModal.show');await page.locator('#user-username').fill('ui-test');await page.locator('#user-password').fill('synthetic-test-only');await page.locator('#save-user').click();await page.waitForSelector('#userModal.show',{state:'hidden'});
                await page.locator('#trusted-tab').click();await page.locator('[data-bs-target="#trustedEmailModal"]').click();await page.locator('#trusted-email-form [name="email"]').fill('new-trusted@example.invalid');await page.locator('#save-trusted-email').click();await page.waitForSelector('#trustedEmailModal.show',{state:'hidden'});
                assert.ok(fixture.state.calls.some(c=>c.path.endsWith('/auth/register')&&c.method==='POST'));assert.ok(fixture.state.calls.some(c=>c.path.endsWith('/settings/trusted-emails')&&c.method==='POST'));
            });
            await checks('hostile API text stays inert and full-value dialog is text only',async()=>{
                await page.evaluate(()=>{document.querySelector('#objects-table tbody').innerHTML='<tr><td>'+admin.compactValue('<img src=x onerror="window.uiXss=true">','Тест')+'</td></tr>';});
                await page.locator('#objects-tab').click();await page.locator('#objects-table [data-value]').click();assert.equal(await page.locator('#full-value img').count(),0);assert.equal(await page.evaluate(()=>Boolean(window.uiXss)),false);
                await page.locator('#valueModal .btn-close').click();await page.waitForSelector('#valueModal.show',{state:'hidden'});
            });
            await checks('empty filtered table displays a clear state',async()=>{
                await page.locator('#search-objects').fill('нет-такого-объекта');await page.waitForSelector('#objects-table .empty-state');assert.ok((await page.locator('#objects-table tbody').innerText()).includes('Ничего не найдено'));
            });
            await checks('operator sees no settings or sender commands',async()=>{
                fixture.state.role='operator';const operator=await context.newPage();await operator.goto(origin+'/after/');await operator.waitForFunction(()=>document.querySelector('#username').textContent==='Администратор'&&document.querySelectorAll('#documents-table tbody tr').length>0);
                assert.equal(await operator.locator('#settings-tab').isVisible(),false);assert.equal(await operator.locator('button[onclick*="reprocessAttachment"],button[onclick*="resendAttachment"]').count(),0);await operator.close();fixture.state.role='admin';
            });
        }
        await checks('no browser errors and no unexpected external requests',async()=>{assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);});
        if(phase==='after')await checks('no SMTP/reprocess/resend API effects',async()=>assert.equal(fixture.state.calls.filter(c=>/\/(resend|reprocess)$/.test(c.path)).length,0));
        await context.close();
    }catch(error){
        results.push({name:'browser scenario completed',status:'FAIL',error:error.message});
        throw error;
    }finally{
        if(browser)await browser.close();await new Promise(resolve=>fixture.server.close(resolve));
        const report={phase,engine,status:results.every(x=>x.status==='PASS')?'PASS':'FAIL',checks:results,screenshots,metrics,errors,unexpectedExternalRequests:unexpected,productionAccess:false,syntheticDataOnly:true};
        fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2)+'\n');
        console.log(JSON.stringify({phase,engine,status:report.status,checks:results.length,screenshots:screenshots.length}));
        if(report.status==='FAIL')process.exitCode=1;
    }
})().catch(error=>{console.error(error.stack);process.exitCode=1;fixture.server.close();});
