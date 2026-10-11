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
    CALLS.push([m,p]);
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
  await check('bot tab',()=>S.showTab('bot'),['#bot','BRIDGE_TIMEOUT_S']);
  await check('deploy panel',async()=>{ await S.refreshDeploy?.(); S.openDeploy(); },['#deploy','Deploy now']);
  await check('selftest pill',()=>S.runSelftest());
  process.stdout.write(JSON.stringify({results,calls:CALLS}));
  process.exit(0);
})();
