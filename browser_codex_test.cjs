const {puppeteer, chromePath, closeBrowser} = require('./browser_test_helpers.cjs');
const assert = require('node:assert/strict');
const {execFileSync, spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const artifacts = path.join(__dirname, '.inspector-data/codex-verification');
let fixtureRoot;
function fixture(){execFileSync('python3',['-c',String.raw`
import json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]);(p/'sessions').mkdir(parents=True,exist_ok=True)
db=p/'state_5.sqlite'
if db.exists():db.unlink()
c=sqlite3.connect(db);c.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,cwd TEXT,model TEXT,archived INTEGER,source TEXT,agent_path TEXT,tokens_used INTEGER,rollout_path TEXT)')
f=p/'sessions/test.jsonl';lines=[]
def add(t,p):lines.append(json.dumps({'type':t,'timestamp':'2026-11-02T07:00:00Z','payload':p}))
add('session_meta',{'base_instructions':{'text':'Base instructions'}})
add('event_msg',{'type':'task_started','turn_id':'turn','model_context_window':1000})
add('turn_context',{'model':'gpt-6-astra','turn_id':'turn','effort':'high'})
for i in range(135):
 add('response_item',{'type':'message','role':'user','content':[{'text':f'Question {i} <script>window.pwned=1</script>'}]})
 add('response_item',{'type':'function_call_output','call_id':str(i),'output':'Result '+str(i)})
 add('token_usage_record',{'response_id':str(i),'usage':{'input_tokens':100+i,'cached_input_tokens':80,'output_tokens':5,'reasoning_output_tokens':2,'total_tokens':105+i},'thread_token_usage':{'input_tokens':10000+i,'output_tokens':500,'total_tokens':10500+i}})
add('compacted',{'replacement_history':[{'type':'message','role':'user','content':[{'text':'Saved during compaction'}]}]})
add('response_item',{'type':'function_call_output','output':'Large result '+('x'*20000)+' END'})
add('token_usage_record',{'response_id':'last','usage':{'input_tokens':50,'cached_input_tokens':20,'output_tokens':5,'reasoning_output_tokens':1,'total_tokens':55},'thread_token_usage':{'input_tokens':11000,'output_tokens':550,'total_tokens':11550}})
f.write_text('\n'.join(lines)+'\n')
for i in range(2000):
 c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)',(f'chat-{i}',f'Task {i}',2000-i,'/Projects/Alpha' if i<10 else '/Projects/Beta','gpt-6-astra',int(i==1),'cli','/root/child' if i==2 else '/root',11550,str(f)))
c.commit();c.close()
` ,fixtureRoot],{cwd:__dirname});}
(async()=>{
fixtureRoot=fs.mkdtempSync(path.join(os.tmpdir(),'murmuratio-codex-'));
fs.mkdirSync(artifacts,{recursive:true});
fixture();
const fixtureConfig = path.join(fixtureRoot, 'config.toml');
fs.writeFileSync(fixtureConfig, '[modules]\ncodex_chats = true\ncontext_reset = true\n');
const service=spawn('python3',['-B','server.py','--port','0','--config',fixtureConfig,'--codex-home',fixtureRoot,'--data-dir',path.join(fixtureRoot,'reset-state')],{cwd:__dirname,env:{...process.env,HOME:fixtureRoot,PORTLESS_URL:''},stdio:['ignore','pipe','pipe']});
let browser;
try {
 const base=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(new Error('Fixture server did not start')),10000);
   service.once('error',error=>{clearTimeout(timer);reject(error);});
   service.once('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture server exited: ${code}`));});
   service.stdout.on('data',chunk=>{const match=String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);if(match){clearTimeout(timer);resolve(match[0]);}});
 });
 browser=await puppeteer.launch({executablePath:chromePath(),headless:true,args:['--disable-background-networking','--disable-sync','--disable-extensions']});
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setViewport({width:1440,height:1100,deviceScaleFactor:1});
 await page.evaluateOnNewDocument(()=>localStorage.setItem('inspector.source','codex'));
 await page.setRequestInterception(true);
 page.on('request',request=>new URL(request.url()).pathname==='/api/now'
   ? request.respond({status:200,contentType:'application/json',body:JSON.stringify({now:Date.now()/1000,agents:[],completed:[]})})
   : request.continue());
 await page.goto(base,{waitUntil:'networkidle0'});await page.waitForSelector('.chat-row');
 assert.equal(await page.$eval('html',x=>x.lang),'en');
 assert.equal(await page.$$eval('.chat-row',x=>x.length),60);
 assert.equal(await page.$eval('#chat-count',x=>x.textContent),'60 of 1998');
 await page.click('#more-chats');assert.equal(await page.$$eval('.chat-row',x=>x.length),120);
 await page.type('#chat-search','Alpha');assert.equal(await page.$$eval('.chat-row',x=>x.length),8);
 await page.click('#show-archive');await page.click('#show-agents');assert.equal(await page.$$eval('.chat-row',x=>x.length),10);
 assert.equal(await page.$('.favorite'),null);
 await page.focus('#sidebar-resizer');await page.keyboard.press('ArrowRight');
 assert.equal(await page.$eval('#sidebar-resizer',x=>x.getAttribute('aria-valuenow')),'290');
 await page.click('.chat-button');await page.waitForSelector('#analysis:not([hidden])');
 assert.deepEqual(await page.$$eval('.metric-title',es=>es.map(e=>e.textContent)),['Context','Usage','Model','Compactions']);
 assert.equal(await page.$eval('#analysis',x=>x.textContent.includes('5%')),true);
 const snapshot=await page.evaluate(async()=>{const r=await fetch('/api/codex/session?id=chat-0');return r.json();});
 assert.equal(snapshot.analysis.compactions[0].before_input,234);
 assert.equal(snapshot.analysis.compactions[0].after_input,50);
 assert.equal(await page.$eval('[data-metric=compaction] .metric-number',x=>x.textContent),'50');
 assert.equal(await page.$eval('[data-metric=compaction]',x=>x.textContent.includes('Total compactions 1')),true);
 assert.equal(await page.evaluate(()=>window.pwned),undefined);
 assert.equal(await page.$$eval('#analysis > .metric-block',x=>x.length),4);
 assert.equal(await page.$eval('#event-log',x=>x.open),false);
 assert.equal(await page.$('#chat-tokens'),null);
 await page.click('#event-log-summary');
 assert.equal(await page.$$eval('.packet',x=>x.length),120);
 await page.$$eval('.packet button',els=>els.find(x=>x.textContent==='Show full text').click());
 await page.waitForFunction(()=>[...document.querySelectorAll('.packet pre')].some(x=>x.textContent.endsWith('END')));
 const before=await page.$$eval('.packet',x=>x.length);await page.click('#older-events');await page.waitForFunction(n=>document.querySelectorAll('.packet').length>n,{},before);
 assert.equal(await page.$$eval('.packet',x=>x.length),240);
 await page.$$eval('#analysis > .metric-block > .analysis-fold',els=>els.forEach(x=>x.open=true));
 await page.screenshot({path:path.join(artifacts,'codex-wide.png'),fullPage:true});
 await page.reload({waitUntil:'networkidle0'});await page.waitForSelector('#analysis:not([hidden])');
 assert.equal(await page.$$eval('.chat-row',x=>x.length),10);assert.equal(await page.$eval('#sidebar-resizer',x=>x.getAttribute('aria-valuenow')),'290');
 await page.click('#tab-now');await page.waitForFunction(()=>document.querySelector('#tab-now').getAttribute('aria-selected')==='true');
 await page.click('#tab-codex');await page.waitForSelector('#analysis:not([hidden])');
 assert.equal(await page.$eval('#session-title',x=>x.textContent),'Task 0');
 await page.setViewport({width:390,height:844,deviceScaleFactor:2});
 await page.$$eval('#analysis > .metric-block > .analysis-fold',els=>els.forEach(x=>x.open=true));
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.screenshot({path:path.join(artifacts,'codex-narrow.png'),fullPage:true});
 await page.click('#reset-context');
 await page.waitForSelector('#reset-dialog[open]');
 assert.equal(await page.$eval('#reset-title',x=>x.textContent),'Create a new context window?');
 await page.screenshot({path:path.join(artifacts,'reset-narrow.png'),fullPage:true});
 await page.setViewport({width:1440,height:1100,deviceScaleFactor:1});
 await page.screenshot({path:path.join(artifacts,'reset-wide.png'),fullPage:true});
 await page.click('#reset-cancel');
 await page.focus('#tab-codex');await page.keyboard.press('ArrowRight');
 assert.equal(await page.$eval('#tab-now',x=>x.getAttribute('aria-selected')),'true');
 await page.keyboard.press('ArrowLeft');assert.equal(await page.$eval('#tab-codex',x=>x.getAttribute('aria-selected')),'true');
 await page.waitForSelector('.chat-row');assert.equal(await page.$('.favorite'),null);
 const noReset = await browser.newPage();
 let resetRequests = 0;
 // Keep this legacy input to verify Russian error-status parsing.
 const statuses = ['Ошибка','Error',null];
 const legacySession = {...snapshot,revision:'legacy:1',next_before:null,event_count:statuses.length,
   events:statuses.map((status,index)=>({id:'tool-'+index,index,kind:'tool_result',status,
     group_id:'turn-'+index,blocks:[{text:'Tool response'}]}))};
 await noReset.evaluateOnNewDocument(()=>localStorage.setItem('inspector.source','codex'));
 await noReset.setRequestInterception(true);
 noReset.on('request',request=>{
   const endpoint = new URL(request.url()).pathname;
   if (endpoint.startsWith('/api/codex/reset/')) resetRequests++;
   if (endpoint === '/api/config') request.respond({status:200,contentType:'application/json',body:JSON.stringify({
     modules:{codex_chats:true,context_reset:false},project_order:[],work_hours:[9,21]})});
   else if (endpoint === '/api/codex/session') request.respond({status:200,contentType:'application/json',body:JSON.stringify(legacySession)});
   else request.continue();
 });
 await noReset.goto(base+'/?codex=chat-0',{waitUntil:'networkidle0'});
 await noReset.waitForSelector('#analysis:not([hidden])');
 assert.equal(await noReset.$eval('#reset-context',e=>e.hidden),true);
 assert.equal(resetRequests,0,'disabled reset never polls the backend');
 assert.deepEqual(await noReset.$$eval('.packet-label',es=>es.map(e=>e.textContent)),['← Tool error','← Tool error','← Tool result']);
 assert.equal(await noReset.$$eval('.packet-label.error',es=>es.length),2);
 assert.deepEqual(await noReset.$$eval('.answers > summary',es=>es.map(e=>e.textContent)),['Reply contains an error','Reply contains an error','Reply']);
 await noReset.close();
 assert.deepEqual(errors,[]);
 console.log('PASS: 2000 chats, bounded list, project search, archive/agents, sidebar width persistence, source tabs/keyboard, context/cache/compaction, pagination, safe text, wide/narrow DPR1/2, no JS errors');
}finally{
 if(browser){for(const page of await browser.pages())await page.close();await closeBrowser(browser);}
 if(service.exitCode===null){const exited=new Promise(resolve=>service.once('exit',resolve));service.kill();await exited;}
 service.stdout.destroy();service.stderr.destroy();
 fs.rmSync(fixtureRoot,{recursive:true,force:true});
}
})().catch(e=>{console.error(e);process.exitCode=1});
