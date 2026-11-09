/* Reproduce a warm V1 browser cache without using production or credentials. */
const {chromium}=require('playwright');
const {createFixture}=require('./fixture.cjs');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const {once}=require('node:events');
const root=path.resolve(__dirname,'../..'),f=createFixture(root),original=f.server.listeners('request')[0];
f.server.removeAllListeners('request');
let oldLoads=0,newLoads=0;
f.server.on('request',(req,res)=>{
    if(req.url==='/warm'){res.writeHead(200,{'Content-Type':'text/html'});res.end('<script src="/after/app.js"></script>');return;}
    if(req.url==='/after/app.js'){oldLoads++;res.writeHead(200,{'Content-Type':'text/javascript','Cache-Control':'public, max-age=86400'});res.end('window.isLegacyBundle=true;');return;}
    if(req.url==='/after/app.js?v=6b8fef4')newLoads++;
    original(req,res);
});
(async()=>{
    f.server.listen(0,'127.0.0.1');await once(f.server,'listening');let browser;
    try{
        const base='http://127.0.0.1:'+f.server.address().port;
        browser=await chromium.launch({channel:'msedge',headless:true});const page=await browser.newPage();
        await page.addInitScript(()=>localStorage.setItem('token','ui-fixture-only'));
        await page.goto(base+'/warm');assert.equal(await page.evaluate(()=>window.isLegacyBundle),true);
        await page.goto(base+'/after/');await page.waitForFunction(()=>document.querySelectorAll('#objects-table .cell-value').length>0&&document.querySelector('#pipeline-alerts').textContent.includes('Pipeline v2:'));
        assert.equal(await page.evaluate(()=>Boolean(window.isLegacyBundle)),false);
        assert.equal(await page.locator('#objects-table .identifier').first().innerText(),'cb962740…0001');
        assert.equal(oldLoads,1);assert.equal(newLoads,1);
        const result={status:'PASS',legacyScriptCached:true,versionedV2ScriptLoaded:true,uuidCompact:true,pipelineStatusRendered:true,productionAccess:false};
        fs.writeFileSync(path.join(root,'evidence/ui/cache-cutover.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
    }finally{if(browser)await browser.close();await new Promise(resolve=>f.server.close(resolve));}
})().catch(e=>{console.error(e.message);process.exitCode=1;f.server.close();});
