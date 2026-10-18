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
  await check('who asked shows by name, the ID on hover',async()=>{
    const r=RESP['/conversations'], u=r.threads[0].users[0];
    if(!u) throw new Error('the captured thread has no asker');
    RESP['/conversations']={...r,names:{[u]:'Gal Badichi'},names_problem:'the Slack app needs the users:read permission'};
    await S.loadConvos(); RESP['/conversations']=r; },
    ['#convos','Gal Badichi','title="U1"','Names: the Slack app needs the users:read permission']);
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
  await check('a viewer gets no change button',async()=>{
    if(el('#bot').innerHTML.includes('change settings')) throw new Error('shown to a viewer'); },['#bot','read-only: ']);
  await check('an admin changes settings',()=>{ S.BOT.b.can_edit=true; S.editSettings(true); },
    ['#bot','id="bs-BRIDGE_EFFORT"','id="bs-sites"','6-digit code']);
  await check('only what changed is sent',()=>{
    // (The stand-in page's boxes start empty, and the form remembered them so.)
    el('#bs-BRIDGE_EFFORT').value='high'; el('#bs-SLACK_ALLOWED_USERS').value='U0AAAAAAA  U0BBBBBBB';
    const c=S.settingChanges();
    if(JSON.stringify(c)!==JSON.stringify({BRIDGE_EFFORT:'high',SLACK_ALLOWED_USERS:'U0AAAAAAA,U0BBBBBBB'})) throw new Error(JSON.stringify(c));
    el('#bs-sites').checked=false; el('#bs-BRIDGE_ALLOWED_HOSTS').value='lab-pc-1';
    if(S.settingChanges().BRIDGE_ALLOWED_HOSTS!=='lab-pc-1') throw new Error('hosts');
    S.editSettings(false); });
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
  await check('categories start collapsed',async()=>{
    S.ACC.open=new Set(['gotcha5']); S.renderAccess();
    const h=el('#sec-access').innerHTML;
    if(!h.includes('<details class="card sys" open')||h.includes('<details class="cat" open')) throw new Error('not as expected'); });
  await check('expand all opens systems and categories',()=>S.expandAll(true),['#sec-access','<details class="cat" open']);
  await check('collapse all closes them',async()=>{ S.expandAll(false);
    if(el('#sec-access').innerHTML.includes('<details class="cat" open')) throw new Error('a category stayed open');
    S.expandAll(true); });
  await check('collapse all also closes a form and the tower view',async()=>{
    S.openForm('comp-edit','gotcha5','1'); S.toggleTowerView('gotcha5'); S.expandAll(false);
    const h=el('#sec-access').innerHTML;
    if(S.ACC.form||S.ACC.towerView.size||h.includes('<details class="card sys" open')||h.includes('<details class="cat" open'))
      throw new Error('something stayed open');
    S.expandAll(true); });
  await check('a sensor row: version, web link, aim, its computer',async()=>{
    const h=el('#sec-access').innerHTML;
    for(const want of ['v4.2','href="http://10.0.0.1/ui"','az 120°','el -2°','<td class="mono">pc</td>','band'])
      if(!h.includes(want)) throw new Error('lacks '+want); });
  await check('a computer row: model, serves, web, RustDesk, users',async()=>{
    const h=el('#sec-access').innerHTML;
    for(const want of ['NUC 13','<b>serves</b> 1','http://10.0.0.50:9090','<b>RustDesk</b>','<b>user</b> <span class="mono">root','viewer'])
      if(!h.includes(want)) throw new Error('lacks '+want); });
  await check('a system shows where it stands, with a map link',async()=>{},['#sec-access','href="https://www.openstreetmap.org/?mlat=32.6619&amp;mlon=35.0102']);
  await check('the tower view is a button on each system',async()=>{},['#sec-access','>tower view</button>']);
  await check('the tower view draws the system as one circle',()=>S.toggleTowerView('gotcha5'),['#sec-access','aria-label="Tower view of gotcha5"']);
  await check('each sensor points along its azimuth',async()=>{
    const h=el('#sec-access').innerHTML;
    // radar 1: azimuth 120, ±45 -> a wedge from 075 to 165, labelled with its direction
    if(!h.includes('>1 120°</text>')) throw new Error('no radar label');
    if(!h.includes('120°, -45° to 45°')) throw new Error('no radar tooltip');
    if(!/<path d="M 150 150 L [\d.]+ [\d.]+ A /.test(h)) throw new Error('no wedge');
    if(!h.includes('north up')) throw new Error('no legend'); });
  await check('and it hides again',()=>S.toggleTowerView('gotcha5'),['#sec-access','>tower view</button>']);
  await check('every secret is masked, none shown',async()=>{
    const h=el('#sec-access').innerHTML;
    if(!h.includes('••••••••')) throw new Error('no masked secret');
    for(const v of ['guipw','webpw','pcweb','>rd<','rootpw','>ssh<']) if(h.includes(v)) throw new Error('shows '+v); });
  await check('reveal the GUI password',async()=>{ const p=S.revealAccess('gotcha5','1','slot:gui_password');
    await tick(); await tick(); el('#codein').value='123456'; if(el('#codeok').onclick) el('#codeok').onclick(); await p; },['#acp-0-0-0-g','secretval']);
  await check('reveal a user password',()=>S.revealAccess('gotcha5','pc','user:root'),['#acp-0-3-0-u0','secretval']);
  await check('the reveal asks for that one secret',async()=>{
    const last=CALLS.filter(c=>c[1].endsWith('/reveal')).pop();
    if(JSON.parse(last[2]).user!=='root') throw new Error(last[2]); });
  const form=(vals)=>Object.entries(vals).forEach(([k,v])=>el('#af-'+k).value=v);
  const clear=()=>['name','site','description','type','role','scheme','address','port','user','version','gui','azimuth',
    'elevation','fov_from','fov_to','range','model','web','rustdesk_ip','pw','guipw','webpw','rdpw','code','category','sensor']
    .forEach(k=>el('#af-'+k).value='');
  await check('clicking a row shows all its fields, read-only',async()=>{
    S.closeForm(); S.toggleDetails({target:{closest:()=>null}},'gotcha5','pc');
    const h=el('#sec-access').innerHTML;
    if(!h.includes('<b>RustDesk password</b>')||!h.includes('<b>user root</b>')||h.includes('id="af-')) throw new Error('not the details');
    S.toggleDetails({target:{closest:()=>({})}},'gotcha5','pc');   // a click on its buttons: no change
    if(!el('#sec-access').innerHTML.includes('<b>RustDesk password</b>')) throw new Error('closed by a button');
    S.toggleDetails({target:{closest:()=>null}},'gotcha5','pc');
    if(el('#sec-access').innerHTML.includes('<b>RustDesk password</b>')) throw new Error('still open'); });
  await check('the sensor form',()=>{ S.ACC.can_edit=true; S.openForm('comp-edit','gotcha5','1'); },
    ['#sec-access','web password','id="af-computer_ip"','computer web password']);
  await check('it has azimuth and elevation, no SSH',async()=>{
    const h=el('#sec-access').innerHTML;
    if(!h.includes('af-azimuth')||!h.includes('af-elevation')||h.includes('af-user')) throw new Error('wrong fields'); });
  await check('save a sensor with fields',async()=>{ clear(); const c=S.ACC.systems[0].categories[0].components[0];
    form({name:c.name,version:'4.3',address:'10.0.0.2',port:'8080',gui:c.gui,azimuth:'120',elevation:'-2',fov_from:'-45',fov_to:'45',
          category:'radars',code:'123456'});
    S.ACC.rows.forEach((r,i)=>{ el('#ff-n-'+i).value=r.name; el('#ff-v-'+i).value=r.value; });
    S.ACC.rows.push({name:'snmp community',value:'c1',secret:true});
    await S.saveComponent('gotcha5','1');
    const call=CALLS.filter(x=>x[0]==='PUT'&&x[1].endsWith('/component/1')).pop(), b=JSON.parse(call[2]);
    if(b.version!=='4.3'||b.address!=='10.0.0.2'||'azimuth' in b||'gui' in b) throw new Error(call[2]); },['#sec-access','firmware']);
  await check('the computer form',async()=>{ S.openForm('comp-edit','gotcha5','pc');
    if(el('#sec-access').innerHTML.includes('id="af-role"')) throw new Error('role is still there'); },
    ['#sec-access','RustDesk password','id="af-web_user"']);
  await check('it lists the users and the sensors it can serve',async()=>{
    const h=el('#sec-access').innerHTML;
    if(!h.includes('uu-n-1')||!h.includes('serves sensor')||!h.includes('<option value="1" selected>')) throw new Error('missing'); });
  await check('save a computer: users change one by one',async()=>{ clear(); const c=S.ACC.systems[0].categories[3].components[0];
    form({name:c.name,type:'',role:'',scheme:'',address:c.address,port:'',user:c.user,model:'NUC 14',web:c.web,
          rustdesk_ip:c.rustdesk_ip,sensor:'1',category:'compute',code:'123456'});
    S.ACC.users.forEach((u,i)=>{ el('#uu-n-'+i).value=u.name; el('#uu-p-'+i).value=''; });
    S.ACC.users[1].removed=true; S.ACC.users.push({name:'ops',pw:'opspw'});
    el('#uu-n-2').value='ops'; el('#uu-p-2').value='opspw';
    await S.saveComponent('gotcha5','pc');
    const call=CALLS.filter(x=>x[0]==='PUT'&&x[1].endsWith('/component/pc')).pop(), b=JSON.parse(call[2]);
    if(b.model!=='NUC 14'||JSON.stringify(b.users)!=='{"viewer":null,"ops":"opspw"}'||'sensor' in b) throw new Error(call[2]); });
  await check('a category stays open after its form closes',async()=>{
    S.ACC.openCats=new Set(); S.openForm('comp-add','gotcha5',null,'acoustics'); S.closeForm();
    if(!el('#sec-access').innerHTML.includes('ontoggle="toggleCat(\'gotcha5/acoustics\'') ||
       !S.ACC.openCats.has('gotcha5/acoustics')) throw new Error('closed again'); });
  await check('a new camera gets the sensor form',async()=>{ S.openForm('comp-add','gotcha5',null,'cameras');
    const h=el('#sec-access').innerHTML;
    if(!h.includes('id="af-azimuth"')||h.includes('id="af-user"')) throw new Error('not the sensor form'); },['#sec-access','value="cameras" selected']);
  await check('add a component',async()=>{ clear(); form({name:'cam',address:'10.0.0.3',category:'cameras',code:'123456'});
    await S.saveComponent('gotcha5',null); },['#sec-access','10.0.0.3']);
  await check('delete a component',async()=>{ S.openForm('comp-edit','gotcha5','cam'); clear(); form({code:'123456'});
    await S.deleteComponent('gotcha5','cam'); });
  await check('the add system form',()=>S.openForm('sys-add'),['#sec-access','Add a system']);
  await check('the system form has coordinates',async()=>{},['#sec-access','id="af-latitude"']);
  await check('add a system',async()=>{ clear(); ['latitude','longitude','altitude'].forEach(k=>el('#af-'+k).value='');
    form({name:'gotcha9',site:'Eilat',latitude:'29.55',longitude:'34.95',code:'123456'});
    await S.saveSystem(null);
    const call=CALLS.filter(x=>x[0]==='POST'&&x[1]==='/access/system').pop(), b=JSON.parse(call[2]);
    if(b.latitude!=='29.55'||b.longitude!=='34.95'||'altitude' in b) throw new Error(call[2]); },['#sec-access','gotcha9']);
  await check('the edit system form',()=>S.openForm('sys-edit','gotcha9'),['#sec-access','delete system']);
  await check('save, cancel and delete sit together',async()=>{
    if(/class="sm no grow"/.test(el('#sec-access').innerHTML)) throw new Error('delete is pushed away'); });
  await check('edit a system',async()=>{ clear(); ['latitude','longitude','altitude'].forEach(k=>el('#af-'+k).value='');
    form({name:'gotcha9',site:'Eilat port',code:'123456'});
    await S.saveSystem('gotcha9'); },['#sec-access','Eilat port']);
  await check('delete a system',async()=>{ S.openForm('sys-edit','gotcha9'); clear(); form({code:'123456'});
    await S.deleteSystem('gotcha9'); });
  await check('hiding a revealed secret keeps an open form',async()=>{
    S.ACC.reveal['gotcha5/pc#user:root']='x'; S.openForm('comp-add','gotcha5',null,'compute'); S.hideAccess('gotcha5/pc#user:root'); },['#sec-access','SSH password (optional)']);
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
  await check('outside the editing window a save sends a code',async()=>{
    S.AUTH.edit_until=null; S.openForm('comp-edit','gotcha5','1'); clear();
    const c=S.ACC.systems[0].categories[0].components[0];
    form({name:c.name,version:'9.8',address:c.address,port:String(c.port),gui:c.gui,azimuth:String(c.azimuth),
          elevation:String(c.elevation),fov_from:String(c.fov_from),fov_to:String(c.fov_to),category:'radars',code:'123456'});
    S.ACC.rows.forEach((r,i)=>{ el('#ff-n-'+i).value=r.name; el('#ff-v-'+i).value=r.value; });
    await S.saveComponent('gotcha5','1');
    const call=CALLS.filter(x=>x[0]==='PUT'&&x[1].startsWith('/access/')).pop();
    if(!call||JSON.parse(call[2]).code!=='123456') throw new Error('sent: '+(call&&call[2])); });
  await check('inside the editing window the form needs no code',()=>{
    S.AUTH.edit_until=Date.now()/1000+300; S.openForm('comp-edit','gotcha5','1'); },['#sec-access','editing unlocked']);
  await check('and a save sends none',async()=>{
    clear(); const c=S.ACC.systems[0].categories[0].components[0];
    form({name:c.name,version:'9.9',address:c.address,port:String(c.port),gui:c.gui,azimuth:String(c.azimuth),
          elevation:String(c.elevation),fov_from:String(c.fov_from),fov_to:String(c.fov_to),category:'radars'});
    S.ACC.rows.forEach((r,i)=>{ el('#ff-n-'+i).value=r.name; el('#ff-v-'+i).value=r.value; });
    const before=CALLS.length;
    await S.saveComponent('gotcha5','1');
    if(CALLS.length===before) throw new Error('no request was sent');
    const call=CALLS.filter(x=>x[0]==='PUT'&&x[1].startsWith('/access/')).pop();
    if(!call||'code' in JSON.parse(call[2])) throw new Error('sent: '+(call&&call[2]));
    S.AUTH.edit_until=null; });
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
