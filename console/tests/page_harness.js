// Runs console/index.html's inline script under node, against a DOM stub and canned API
// answers, and drives every tab and editor. The page has no build step and no browser in CI,
// so this is what catches a typo in a template literal before it blanks a tab.
//
//   node page_harness.js slugify '["Some name", ...]'      -> JSON slugs, for test_cases.py
//   node page_harness.js render responses.json             -> JSON [[ok|FAIL, label, why?]]
// responses.json maps an API path (without /api, query string dropped) to its JSON answer,
// captured from the real server by test_page.py.
process.on('unhandledRejection',e=>{ERRORS.push(String(e&&e.stack||e));});
const ERRORS=[];
const fs=require('fs'), vm=require('vm'), path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','index.html'),'utf8');
const js=html.split('</div><script>')[1].split('</script>')[0];
const [mode,arg]=process.argv.slice(2);
const RESP=mode==='render'?JSON.parse(fs.readFileSync(arg,'utf8')):{};

const mk=sel=>({sel,innerHTML:'',textContent:'',value:'',hidden:false,dataset:{},style:{},className:'',
  classList:{add(){},remove(){},toggle(){},contains(){return false}},
  scrollIntoView(){},focus(){},setAttribute(){},getAttribute(){return ''},addEventListener(){},
  getBoundingClientRect:()=>({left:0,top:0,width:100}),querySelector:()=>null,querySelectorAll:()=>[]});
const els={};
const el=sel=>els[sel]||(els[sel]=mk(sel));
const CALLS=[];
const sandbox={console,setTimeout:(f)=>0,clearTimeout(){},Promise,JSON,Math,Number,Object,Date,String,Array,Set,
  URLSearchParams,encodeURIComponent,decodeURIComponent,alert:m=>{ if(!/^Renamed/.test(m)) ERRORS.push('alert: '+m); },confirm:()=>true,prompt:()=>'renamed case',
  location:{hash:''},history:{replaceState(_,__,h){sandbox.location.hash=h}},
  fetch:(url,opts)=>{const p=decodeURIComponent(url.replace(/^\/api/,'').split('?')[0]), m=opts&&opts.method||'GET';
    CALLS.push([m,p,opts&&opts.body]);
    if(m!=='GET'&&!(opts.headers&&opts.headers['x-console']==='1')) ERRORS.push(`${m} ${p} without X-Console`);
    const body=RESP[m+' '+p]??(m==='GET'?RESP[p]:undefined);
    if(body===undefined){ ERRORS.push(`no canned answer for ${m} ${p}`); return Promise.resolve({ok:false,json:async()=>({detail:'not canned'})}); }
    return Promise.resolve({ok:true,json:async()=>JSON.parse(JSON.stringify(body))});},
  document:{querySelector:sel=>el(sel),querySelectorAll:()=>[],getElementById:id=>el('#'+id)}};
sandbox.window=sandbox;
vm.createContext(sandbox);
vm.runInContext(js,sandbox);

if(mode==='slugify'){
  process.stdout.write(JSON.stringify(JSON.parse(arg).map(sandbox.slugify)));
  process.exit(0);
}

const results=[];
const tick=()=>new Promise(r=>setImmediate(r));
async function check(label,fn,expect){
  const before=ERRORS.length;
  try{ await fn(); for(let i=0;i<5;i++) await tick();
    if(ERRORS.length>before) throw new Error(ERRORS.slice(before).join('\n'));
    if(expect){ const [sel,needle]=expect; const h=el(sel).innerHTML;
      if(!h.includes(needle)) throw new Error(`${sel} lacks ${JSON.stringify(needle)}: ${h.slice(0,300)}`); }
    results.push(['ok',label]); }
  catch(e){ results.push(['FAIL',label,String(e&&e.stack||e)]); }
}
const S=sandbox;
(async()=>{
  await check('boot',async()=>{},['#who','']);
  await check('conversations tab',()=>S.showTab('convos'),['#convos','Threads']);
  const key=RESP['/conversations'].threads[0].key;
  await check('open a thread',()=>S.openThread(key),['#cv-detail','Turn 1']);
  await check('a thread shows the steps',async()=>{},['#cv-detail','What the bot did']);
  await check('a timed-out turn says so',async()=>{},['#cv-detail','time limit']);
  await check('cases tab',()=>S.showTab('cases'),['#cases','Cases']);
  await check('open a case in the form',()=>S.openCase('radar-transmitter-off'),['#case-ed','SKILL.md index row']);
  await check('the form has the sections',async()=>{},['#case-ed','Root cause']);
  await check('signatures render',()=>S.renderSigs(),['#case-sigs','radar=stopped']);
  await check('symptoms render',()=>S.renderSyms(),['#case-syms','<input']);
  await check('add a section',()=>{ el('#sec-add').value='Verify'; S.addSec(); },['#case-ed','Verify']);
  await check('move and remove a section',()=>{ S.moveSec(0,1); S.rmSec(0); });
  await check('switch to markdown',async()=>{ await S.setMode('raw'); },['#case-ed','case-raw']);
  await check('save a case',()=>S.saveCase());
  await check('a case with no index row warns',async()=>{
    RESP['/cases/radar-transmitter-off']={...RESP['/cases/radar-transmitter-off'],index:null};
    await S.openCase('radar-transmitter-off');},['#case-ed','Not in the SKILL.md case index']);
  await check('new case',()=>{ el('#case-new').value='Radar flaps'; return S.newCase(); });
  await check('rename',()=>S.renameCase());
  await check('delete',()=>S.deleteCase());
  await check('docs tab',()=>S.showTab('docs'),['#docs','glossary']);
  await check('open a doc',()=>S.openDoc('glossary'),['#doc-ed','textarea']);
  await check('preview a doc',()=>{ S.DOC.preview=true; S.renderDoc(); },['#doc-ed','class="md"']);
  await check('save a doc',()=>{ S.DOC.preview=false; S.renderDoc(); return S.saveDoc(); });
  await check('SKILL.md is read-only',()=>S.openDoc('skill'),['#doc-ed','readonly']);
  await check('stats tab',()=>S.showTab('stats'),['#stats','Questions per day']);
  await check('stats chart has columns',async()=>{},['#stats','class="mark"']);
  await check('systems tab',()=>S.showTab('systems'),['#systems','axon-gotcha-4']);
  await check('open the sensors of a site',()=>S.toggleSite('axon-gotcha-4'),['#systems','magos_radar']);
  await check('a site shows how each address answered',async()=>{},['#systems','answers (ping)']);
  await check('bot tab',()=>S.showTab('bot'),['#bot','BRIDGE_TIMEOUT_S']);
  await check('deploy panel',async()=>{ await S.refreshDeploy?.(); S.openDeploy(); },['#deploy','Deploy now']);
  await check('selftest pill',()=>S.runSelftest());
  await check('secrets tab hides values',()=>S.showTab('secrets'),['#sec-bot','••••••••']);
  await check('an admin sees reveal and change',async()=>{},['#sec-bot','reveal</button>']);
  await check('a viewer sees no buttons',()=>{ S.SEC.can_edit=false; S.renderSecrets(); },['#sec-bot','Ask an admin']);
  await check('reveal after a code',async()=>{ S.SEC.can_edit=true;
    el('#codein').value='123456'; const p=S.revealSecret('SLACK_BOT_TOKEN');
    await tick(); el('#codeok').onclick(); await p; },['#sec-bot','secretval']);
  await check('change form asks for a code',()=>S.editSecret('SLACK_BOT_TOKEN'),['#sec-bot','6-digit code']);
  await check('save a secret',async()=>{ el('#sv-val').value='xoxb-y'; el('#sv-code').value='123456';
    await S.saveSecret('SLACK_BOT_TOKEN',false); });
  await check('add a secret form',()=>S.addSecret(),['#sec-bot','NAME_OF_SECRET']);
  await check('the secrets tab also shows system access',async()=>{},['#sec-access','10.0.0.1']);
  await check('the four categories',async()=>{},['#sec-access','Compute &amp; Infrastructure']);
  await check('systems are collapsible',()=>S.expandAll(false),['#sec-access','<details class="card sys" ']);
  await check('expand all opens them',()=>S.expandAll(true),['#sec-access','<details class="card sys" open']);
  await check('a plain extra field shows',async()=>{},['#sec-access','azimuth']);
  await check('passwords and secret fields are masked',async()=>{},['#sec-access','••••••••']);
  await check('reveal the SSH password',async()=>{ el('#codein').value='123456'; const p=S.revealAccess('gotcha5','1',null);
    await tick(); await tick(); if(el('#codeok').onclick) el('#codeok').onclick(); await p; },['#acp-0-0-0','secretval']);
  await check('reveal a secret field',()=>S.revealAccess('gotcha5','1','web password'),['#acp-0-0-0-f0','secretval']);
  const form=(vals)=>Object.entries(vals).forEach(([k,v])=>el('#af-'+k).value=v);
  const clear=()=>['name','site','description','type','role','scheme','address','port','user','pw','code','category']
    .forEach(k=>el('#af-'+k).value='');
  await check('the edit component form',()=>{ S.ACC.can_edit=true; S.openForm('comp-edit','gotcha5','1'); },['#sec-access','delete component']);
  await check('the form lists its extra fields',async()=>{},['#sec-access','ff-n-1']);
  await check('add an extra field row',()=>{ el('#ff-n-0').value='azimuth'; el('#ff-v-0').value='125';
    S.syncRows(); S.ACC.rows.push({name:'firmware',value:'4.2',secret:false}); S.renderAccess(); },['#sec-access','ff-n-2']);
  await check('save a component with fields',async()=>{ clear(); const c=S.ACC.systems[0].categories[0].components[0];
    form({name:c.name,type:c.type||'',address:'10.0.0.2',user:c.user||'',category:'radars',code:'123456'});
    S.ACC.rows.push({name:'snmp community',value:'c1',secret:true});
    await S.saveComponent('gotcha5','1'); },['#sec-access','firmware']);
  await check('the add form picks its category',()=>S.openForm('comp-add','gotcha5',null,'cameras'),['#sec-access','value="cameras" selected']);
  await check('add a component',async()=>{ clear(); form({name:'cam',type:'camera',address:'10.0.0.3',category:'cameras',code:'123456'});
    await S.saveComponent('gotcha5',null); },['#sec-access','10.0.0.3']);
  await check('delete a component',async()=>{ S.openForm('comp-edit','gotcha5','cam'); clear(); form({code:'123456'});
    await S.deleteComponent('gotcha5','cam'); });
  await check('the add system form',()=>S.openForm('sys-add'),['#sec-access','Add a system']);
  await check('add a system',async()=>{ clear(); form({name:'gotcha9',site:'Eilat',code:'123456'});
    await S.saveSystem(null); },['#sec-access','gotcha9']);
  await check('the edit system form',()=>S.openForm('sys-edit','gotcha9'),['#sec-access','delete system']);
  await check('edit a system',async()=>{ clear(); form({name:'gotcha9',site:'Eilat port',code:'123456'});
    await S.saveSystem('gotcha9'); },['#sec-access','Eilat port']);
  await check('delete a system',async()=>{ S.openForm('sys-edit','gotcha9'); clear(); form({code:'123456'});
    await S.deleteSystem('gotcha9'); });
  await check('hiding a revealed secret keeps an open form',async()=>{
    S.ACC.reveal['gotcha5/1']='pw'; S.openForm('comp-add','gotcha5',null,'radars'); S.hideAccess('gotcha5/1'); },['#sec-access','SSH password (optional)']);
  await check('an untouched multi-line field goes back exactly',()=>{
    const orig={fields:{multi:'x\ny',pad:' p ',n:'1'}};
    S.ACC.rows=[{name:'multi',value:'xy',secret:false,existing:true,origName:'multi',raw:'x\ny'},
                {name:'pad',value:'p',secret:false,existing:true,origName:'pad',raw:' p '},
                {name:'n',value:'2',secret:false,existing:true,origName:'n',raw:'1'}];
    S.ACC.rows.forEach((r,i)=>{ el('#ff-n-'+i).value=r.name; el('#ff-v-'+i).value=r.value; });
    const out=S.fieldChanges(orig);
    if(out.fields.multi!=='x\ny'||out.fields.pad!==' p '||out.fields.n!=='2') throw new Error(JSON.stringify(out)); });
  await check('nothing changed sends no fields',()=>{
    S.ACC.rows=[{name:'multi',value:'xy',secret:false,existing:true,origName:'multi',raw:'x\ny'}];
    el('#ff-n-0').value='multi'; el('#ff-v-0').value='xy';
    const out=S.fieldChanges({fields:{multi:'x\ny'}});
    if('fields' in out) throw new Error(JSON.stringify(out)); });
  await check('passkeys panel',()=>{ S.closeForm(); return S.togglePasskeys(); },['#pkpanel','Your passkeys']);
  await check('passkeys need a secure page',async()=>{},['#pkpanel','localhost']);
  await check('removing a passkey sends a code',async()=>{
    RESP['DELETE /passkeys/Y3JlZC0x']={passkeys:[]};
    const p=S.removePasskey('Y3JlZC0x');
    await tick(); await tick(); el('#codein').value='123456'; el('#codeok').onclick(); await p;
    const call=CALLS.filter(c=>c[0]==='DELETE'&&c[1].startsWith('/passkeys/')).pop();
    if(!call) throw new Error('no DELETE /passkeys call');
    if(JSON.parse(call[2]).code!=='123456') throw new Error('sent without the code: '+call[2]); });
  await check('a viewer sees no access buttons',()=>{ S.ACC.can_edit=false; S.renderAccess(); },['#sec-access','ask an admin']);
  process.stdout.write(JSON.stringify({results,calls:CALLS}));
  process.exit(0);
})();
